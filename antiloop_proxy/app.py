from __future__ import annotations

import itertools
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

Mode = Literal["observe", "balanced", "strict"]

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

COMMON_TOKENS = {
    "true",
    "false",
    "null",
    "none",
    "http",
    "https",
    "json",
    "tool",
    "result",
    "error",
    "warning",
    "failed",
    "stderr",
    "stdout",
    "exit",
    "code",
}

SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(token\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)(password\s*[=:]\s*)[^\s,;]+"),
]


@dataclass(slots=True)
class Settings:
    upstream: str = "http://127.0.0.1:8080"
    log_dir: Path = Path("~/antiloop/logs").expanduser()
    mode: Mode = "balanced"
    host: str = "0.0.0.0"
    port: int = 8081
    request_timeout: int = 300
    passthrough_timeout: int = 60
    max_tokens_override: int = 32768
    preview_length: int = 160
    loop_message_lookback: int = 10
    loop_min_tool_results: int = 3
    loop_min_common_tokens: int = 2
    loop_similarity_ratio: float = 1.25
    redact_logs: bool = True
    force_tool_choice_when_tool_last: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        defaults = cls()

        def env_str(name: str, default: str) -> str:
            value = os.getenv(name)
            return value if value not in (None, "") else default

        def env_int(name: str, default: int) -> int:
            value = os.getenv(name)
            if value in (None, ""):
                return default
            return int(value)

        def env_float(name: str, default: float) -> float:
            value = os.getenv(name)
            if value in (None, ""):
                return default
            return float(value)

        def env_bool(name: str, default: bool) -> bool:
            value = os.getenv(name)
            if value in (None, ""):
                return default
            return value.lower() in {"1", "true", "yes", "on"}

        mode = env_str("PROXY_MODE", defaults.mode)
        if mode not in {"observe", "balanced", "strict"}:
            raise RuntimeError("PROXY_MODE must be one of: observe, balanced, strict")

        return cls(
            upstream=env_str("PROXY_UPSTREAM", defaults.upstream).rstrip("/"),
            log_dir=Path(env_str("PROXY_LOG_DIR", str(defaults.log_dir))).expanduser(),
            mode=mode,  # type: ignore[arg-type]
            host=env_str("PROXY_HOST", defaults.host),
            port=env_int("PROXY_PORT", defaults.port),
            request_timeout=env_int("PROXY_REQUEST_TIMEOUT", defaults.request_timeout),
            passthrough_timeout=env_int("PROXY_PASSTHROUGH_TIMEOUT", defaults.passthrough_timeout),
            max_tokens_override=env_int("PROXY_MAX_TOKENS_OVERRIDE", defaults.max_tokens_override),
            preview_length=env_int("PROXY_PREVIEW_LENGTH", defaults.preview_length),
            loop_message_lookback=env_int("PROXY_LOOP_MESSAGE_LOOKBACK", defaults.loop_message_lookback),
            loop_min_tool_results=max(2, env_int("PROXY_LOOP_MIN_TOOL_RESULTS", defaults.loop_min_tool_results)),
            loop_min_common_tokens=max(1, env_int("PROXY_LOOP_MIN_COMMON_TOKENS", defaults.loop_min_common_tokens)),
            loop_similarity_ratio=max(1.0, env_float("PROXY_LOOP_SIMILARITY_RATIO", defaults.loop_similarity_ratio)),
            redact_logs=env_bool("PROXY_REDACT_LOGS", defaults.redact_logs),
            force_tool_choice_when_tool_last=env_bool(
                "PROXY_FORCE_TOOL_CHOICE_WHEN_TOOL_LAST",
                defaults.force_tool_choice_when_tool_last,
            ),
        )


@dataclass(slots=True)
class ProxyState:
    settings: Settings
    request_ids: itertools.count
    log_lock: Lock


@dataclass(slots=True)
class LoopResult:
    detected: bool
    consecutive_similar: int
    reason: str = ""


@dataclass(slots=True)
class RequestMeta:
    tools_count: int
    last_role: str | None
    last_content_preview: str
    tool_choice_original: Any
    tool_choice_effective: Any
    injected: bool
    injection_skipped: bool
    loop_detected: bool
    loop_reason: str
    consecutive_similar: int
    mode: Mode
    stream: bool
    max_tokens_effective: Any


logger = logging.getLogger("antiloop_proxy")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")



def sanitize_text(text: str, redact: bool) -> str:
    if not redact:
        return text
    value = text
    for pattern in SENSITIVE_PATTERNS:
        value = pattern.sub(r"\1<redacted>", value)
    return value



def preview(value: Any, *, length: int, redact: bool) -> str:
    if value is None:
        return ""
    normalized = sanitize_text(str(value).replace("\n", " ").strip(), redact)
    return normalized[:length] + ("..." if len(normalized) > length else "")



def extract_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part.get("input"), str):
                    parts.append(part["input"])
                elif isinstance(part.get("output"), str):
                    parts.append(part["output"])
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "input", "output"):
            if key in value:
                return extract_content(value[key])
    return str(value)



def significant_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9_./:-]{4,}", text)
        if not token.isdigit() and token.lower() not in COMMON_TOKENS
    }



def evaluate_loop(messages: list[dict[str, Any]], settings: Settings) -> LoopResult:
    recent_messages = (
        messages[-settings.loop_message_lookback :]
        if settings.loop_message_lookback > 0
        else messages
    )
    tool_contents = [
        extract_content(message.get("content"))
        for message in recent_messages
        if message.get("role") == "tool"
    ]

    if len(tool_contents) < settings.loop_min_tool_results:
        return LoopResult(False, 0, "")

    latest = tool_contents[-settings.loop_min_tool_results :]
    lengths = [len(item) for item in latest]
    min_len, max_len = min(lengths), max(lengths)
    consecutive_similar = 0
    if min_len == 0 and max_len == 0:
        consecutive_similar = len(latest)
    elif min_len > 0 and max_len / min_len <= settings.loop_similarity_ratio:
        consecutive_similar = len(latest)

    normalized = [item.strip().lower() for item in latest if item.strip()]
    if normalized and len(set(normalized)) == 1:
        return LoopResult(True, consecutive_similar, "recent tool results are textually identical")

    token_sets = [significant_tokens(item) for item in latest if item.strip()]
    if len(token_sets) < settings.loop_min_tool_results:
        return LoopResult(False, consecutive_similar, "")

    common = set.intersection(*token_sets) if token_sets else set()
    if len(common) >= settings.loop_min_common_tokens:
        sample = ", ".join(sorted(common)[:3])
        return LoopResult(True, consecutive_similar, f"common tokens across recent tool results: {sample}")

    return LoopResult(False, consecutive_similar, "")



def sanitize_headers(headers: httpx.Headers | dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in dict(headers).items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }



def choose_tool_policy(
    *,
    body: dict[str, Any],
    settings: Settings,
    loop_result: LoopResult,
) -> tuple[Any, Any, bool, bool]:
    messages = body.get("messages", []) or []
    last_role = messages[-1].get("role") if messages else None
    tools_count = len(body.get("tools", []) or [])
    original = body.get("tool_choice")

    injected = False
    skipped = False
    effective = original

    if settings.mode == "observe":
        return original, effective, injected, skipped

    if tools_count <= 0 or last_role != "tool" or not settings.force_tool_choice_when_tool_last:
        return original, effective, injected, skipped

    if loop_result.detected:
        skipped = True
        return original, effective, injected, skipped

    if settings.mode == "strict":
        effective = "required"
        body["tool_choice"] = effective
        injected = original != effective
        return original, effective, injected, skipped

    if original in (None, "auto"):
        effective = "required"
        body["tool_choice"] = effective
        injected = True

    return original, effective, injected, skipped



def apply_request_policy(body: dict[str, Any], settings: Settings) -> tuple[dict[str, Any], RequestMeta]:
    messages = body.get("messages", []) or []
    last_message = messages[-1] if messages else {}
    loop_result = evaluate_loop(messages, settings)
    original_tool_choice, tool_choice_effective, injected, injection_skipped = choose_tool_policy(
        body=body,
        settings=settings,
        loop_result=loop_result,
    )

    if settings.max_tokens_override > 0 and body.get("max_tokens") in (None, 0, ""):
        body["max_tokens"] = settings.max_tokens_override

    meta = RequestMeta(
        tools_count=len(body.get("tools", []) or []),
        last_role=last_message.get("role"),
        last_content_preview=preview(
            extract_content(last_message.get("content")),
            length=settings.preview_length,
            redact=settings.redact_logs,
        ),
        tool_choice_original=original_tool_choice,
        tool_choice_effective=tool_choice_effective,
        injected=injected,
        injection_skipped=injection_skipped,
        loop_detected=loop_result.detected,
        loop_reason=loop_result.reason,
        consecutive_similar=loop_result.consecutive_similar,
        mode=settings.mode,
        stream=bool(body.get("stream", False)),
        max_tokens_effective=body.get("max_tokens"),
    )
    return body, meta



def build_problem_summary(meta: RequestMeta, finish_reason: str | None, tool_calls_count: int) -> str:
    problem = ""
    if meta.last_role == "tool" and not meta.injection_skipped and finish_reason == "stop":
        problem += "  *** PROBLEM: finish_reason=stop after tool result — upstream may have abandoned tool calling.\n"
    if meta.last_role == "tool" and not meta.injection_skipped and tool_calls_count == 0:
        problem += "  *** PROBLEM: 0 tool_calls after tool result — client may terminate early.\n"
    if finish_reason == "length":
        problem += "  *** WARNING: response truncated due to max_tokens.\n"
    return problem


class DailyLogger:
    def __init__(self, state: ProxyState):
        self.state = state
        self.state.settings.log_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, suffix: str) -> Path:
        date_prefix = datetime.now().strftime("%Y-%m-%d")
        return self.state.settings.log_dir / f"{date_prefix}.{suffix}"

    def human(self, text: str) -> None:
        with self.state.log_lock:
            with self._path("log").open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")

    def jsonl(self, record: dict[str, Any]) -> None:
        with self.state.log_lock:
            with self._path("jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")



def log_request(log_writer: DailyLogger, request_id: int, meta: RequestMeta, messages_count: int) -> None:
    ts = utc_now()
    inject_note = str(meta.tool_choice_effective)
    if meta.injected:
        inject_note = f"{meta.tool_choice_original} -> injected: {meta.tool_choice_effective}"
    elif meta.injection_skipped:
        inject_note = f"{meta.tool_choice_original} -> skipped ({meta.loop_reason})"

    summary = (
        f"\n[{ts}] REQUEST #{request_id}\n"
        f"  messages: {messages_count}, last_role={meta.last_role}, preview=\"{meta.last_content_preview}\"\n"
        f"  tools: {meta.tools_count}, mode={meta.mode}, stream={meta.stream}\n"
        f"  tool_choice: {inject_note}\n"
        f"  max_tokens: {meta.max_tokens_effective}\n"
    )
    if meta.consecutive_similar:
        summary += f"  consecutive_similar_tool_results: {meta.consecutive_similar}\n"

    logger.warning(summary.rstrip())
    log_writer.human(summary.rstrip())
    payload = asdict(meta)
    payload.update({"type": "request", "id": request_id, "timestamp": ts, "messages_count": messages_count})
    log_writer.jsonl(payload)



def log_response(
    log_writer: DailyLogger,
    request_id: int,
    meta: RequestMeta,
    *,
    streamed: bool,
    finish_reason: str | None,
    tool_calls_count: int,
    content_text: str,
    duration_ms: int,
    status_code: int,
    redact: bool,
) -> None:
    ts = utc_now()
    problem = build_problem_summary(meta, finish_reason, tool_calls_count)
    summary = (
        f"[{ts}] RESPONSE #{request_id}{' (stream)' if streamed else ''}\n"
        f"  upstream_status: {status_code}\n"
        f"  finish_reason: {finish_reason}\n"
        f"  tool_calls: {tool_calls_count}\n"
        f"  content_preview: \"{preview(content_text, length=160, redact=redact)}\"\n"
        f"  duration_ms: {duration_ms}\n"
        f"{problem}"
    ).rstrip()
    logger.warning(summary)
    log_writer.human(summary)
    log_writer.jsonl(
        {
            "type": "response_stream" if streamed else "response",
            "id": request_id,
            "timestamp": ts,
            "upstream_status": status_code,
            "finish_reason": finish_reason,
            "tool_calls_count": tool_calls_count,
            "content_preview": preview(content_text, length=160, redact=redact),
            "duration_ms": duration_ms,
            "truncated": finish_reason == "length",
            "problem": bool(problem),
        }
    )



def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    state = ProxyState(settings=settings, request_ids=itertools.count(1), log_lock=Lock())
    log_writer = DailyLogger(state)
    app = FastAPI(title="antiloop-proxy")
    app.state.proxy_state = state

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "upstream": settings.upstream,
            "mode": settings.mode,
            "redact_logs": settings.redact_logs,
        }

    @app.get("/configz")
    async def configz() -> dict[str, Any]:
        payload = asdict(settings)
        payload["log_dir"] = str(settings.log_dir)
        return payload

    @app.post("/v1/chat/completions")
    async def proxy_chat(request: Request):
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Request body must be a JSON object")

        request_id = next(state.request_ids)
        body, meta = apply_request_policy(body, settings)
        log_request(log_writer, request_id, meta, len(body.get("messages", []) or []))
        started = time.monotonic()
        timeout = httpx.Timeout(settings.request_timeout)

        if body.get("stream", False):
            client = httpx.AsyncClient(timeout=timeout)
            req = client.build_request("POST", f"{settings.upstream}/v1/chat/completions", json=body)
            resp = await client.send(req, stream=True)
            if resp.status_code >= 400:
                raw = await resp.aread()
                await resp.aclose()
                await client.aclose()
                duration_ms = int((time.monotonic() - started) * 1000)
                log_response(
                    log_writer,
                    request_id,
                    meta,
                    streamed=False,
                    finish_reason="error",
                    tool_calls_count=0,
                    content_text=raw.decode("utf-8", errors="replace"),
                    duration_ms=duration_ms,
                    status_code=resp.status_code,
                    redact=settings.redact_logs,
                )
                return Response(
                    content=raw,
                    status_code=resp.status_code,
                    headers=sanitize_headers(resp.headers),
                    media_type=resp.headers.get("content-type"),
                )

            async def iterator():
                finish_reason = None
                tool_calls_seen = 0
                content_parts: list[str] = []
                try:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        yield line + "\n\n"
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                data = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            choice = (data.get("choices") or [{}])[0]
                            delta = choice.get("delta", {})
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                            for tool_call in delta.get("tool_calls", []):
                                if tool_call.get("id"):
                                    tool_calls_seen += 1
                            if delta.get("content"):
                                content_parts.append(str(delta["content"]))
                finally:
                    duration_ms = int((time.monotonic() - started) * 1000)
                    log_response(
                        log_writer,
                        request_id,
                        meta,
                        streamed=True,
                        finish_reason=finish_reason,
                        tool_calls_count=tool_calls_seen,
                        content_text="".join(content_parts),
                        duration_ms=duration_ms,
                        status_code=resp.status_code,
                        redact=settings.redact_logs,
                    )
                    await resp.aclose()
                    await client.aclose()

            return StreamingResponse(
                iterator(),
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "text/event-stream"),
                headers={
                    **sanitize_headers(resp.headers),
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{settings.upstream}/v1/chat/completions", json=body)

        duration_ms = int((time.monotonic() - started) * 1000)
        finish_reason = "unknown"
        tool_calls_count = 0
        content_text = resp.text
        try:
            payload = resp.json()
            choice = (payload.get("choices") or [{}])[0]
            finish_reason = choice.get("finish_reason", "unknown")
            tool_calls_count = len(choice.get("message", {}).get("tool_calls") or [])
            content_text = extract_content(choice.get("message", {}).get("content")) or resp.text
        except Exception:
            pass

        log_response(
            log_writer,
            request_id,
            meta,
            streamed=False,
            finish_reason=finish_reason,
            tool_calls_count=tool_calls_count,
            content_text=content_text,
            duration_ms=duration_ms,
            status_code=resp.status_code,
            redact=settings.redact_logs,
        )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=sanitize_headers(resp.headers),
            media_type=resp.headers.get("content-type", "application/json"),
        )

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def passthrough(request: Request, path: str):
        async with httpx.AsyncClient(timeout=settings.passthrough_timeout) as client:
            url = f"{settings.upstream}/{path.lstrip('/')}"
            resp = await client.request(
                method=request.method,
                url=url,
                params=dict(request.query_params),
                headers=sanitize_headers(dict(request.headers)),
                content=await request.body(),
            )
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=sanitize_headers(resp.headers),
            media_type=resp.headers.get("content-type"),
        )

    @app.exception_handler(httpx.HTTPError)
    async def httpx_error_handler(_: Request, exc: httpx.HTTPError):
        return JSONResponse(
            status_code=502,
            content={"error": "upstream_request_failed", "detail": str(exc)},
        )

    return app


app = create_app()
