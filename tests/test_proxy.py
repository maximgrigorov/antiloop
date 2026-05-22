from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from antiloop_proxy.app import Settings, apply_request_policy, create_app, evaluate_loop, preview


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {"content-type": "application/json"}
        self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return self._payload


class FakeAsyncClient:
    last_request: dict | None = None

    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.get("timeout")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, json: dict):
        FakeAsyncClient.last_request = {"url": url, "json": json}
        return FakeResponse(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "ok",
                            "tool_calls": [{"id": "call-1", "function": {"name": "grep_logs", "arguments": "{}"}}],
                        },
                    }
                ],
                "usage": {"completion_tokens": 42},
            }
        )

    async def request(self, method: str, url: str, params=None, headers=None, content=None):
        FakeAsyncClient.last_request = {
            "method": method,
            "url": url,
            "params": params,
            "headers": headers,
            "content": content,
        }
        return FakeResponse({"ok": True})



def base_body() -> dict:
    return {
        "model": "local-model",
        "messages": [
            {"role": "assistant", "content": "running tool"},
            {"role": "tool", "content": "connection refused to postgres-primary"},
        ],
        "tools": [{"type": "function", "function": {"name": "grep_logs", "parameters": {}}}],
    }



def test_observe_mode_does_not_mutate_tool_choice():
    body, meta = apply_request_policy(base_body(), Settings(mode="observe", log_dir=Path("/tmp/antiloop-test")))
    assert "tool_choice" not in body
    assert meta.injected is False
    assert meta.mode == "observe"



def test_balanced_mode_injects_required_after_tool_result():
    body, meta = apply_request_policy(base_body(), Settings(mode="balanced", log_dir=Path("/tmp/antiloop-test")))
    assert body["tool_choice"] == "required"
    assert meta.injected is True
    assert meta.tool_choice_original is None
    assert meta.tool_choice_effective == "required"



def test_strict_mode_overrides_explicit_tool_choice():
    request = base_body()
    request["tool_choice"] = "none"
    body, meta = apply_request_policy(request, Settings(mode="strict", log_dir=Path("/tmp/antiloop-test")))
    assert body["tool_choice"] == "required"
    assert meta.tool_choice_original == "none"
    assert meta.injected is True



def test_loop_detection_skips_injection_when_results_repeat():
    request = {
        "model": "local-model",
        "messages": [
            {"role": "assistant", "content": "run"},
            {"role": "tool", "content": "connection refused postgres-primary"},
            {"role": "assistant", "content": "retry"},
            {"role": "tool", "content": "connection refused postgres-primary"},
            {"role": "assistant", "content": "retry"},
            {"role": "tool", "content": "connection refused postgres-primary"},
        ],
        "tools": [{"type": "function", "function": {"name": "grep_logs", "parameters": {}}}],
    }
    body, meta = apply_request_policy(request, Settings(mode="balanced", log_dir=Path("/tmp/antiloop-test")))
    assert "tool_choice" not in body
    assert meta.injection_skipped is True
    assert meta.loop_detected is True



def test_preview_redacts_sensitive_values():
    text = "Authorization: Bearer super-secret-token password=abc123"
    rendered = preview(text, length=200, redact=True)
    assert "super-secret-token" not in rendered
    assert "abc123" not in rendered
    assert "<redacted>" in rendered



def test_evaluate_loop_ignores_common_noise_tokens():
    settings = Settings(mode="balanced", log_dir=Path("/tmp/antiloop-test"))
    result = evaluate_loop(
        [
            {"role": "tool", "content": "error warning failed stdout"},
            {"role": "tool", "content": "error warning failed stderr"},
            {"role": "tool", "content": "error failed stdout stderr"},
        ],
        settings,
    )
    assert result.detected is False



def test_app_health_and_config_endpoints(tmp_path: Path):
    app = create_app(Settings(log_dir=tmp_path, mode="strict", upstream="http://upstream:8080"))
    client = TestClient(app)
    assert client.get("/healthz").json()["mode"] == "strict"
    assert client.get("/configz").json()["upstream"] == "http://upstream:8080"



def test_non_stream_chat_completion_forwards_with_policy(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    app = create_app(Settings(log_dir=tmp_path, mode="balanced", upstream="http://upstream:8080"))
    client = TestClient(app)

    response = client.post("/v1/chat/completions", json=base_body())
    assert response.status_code == 200
    assert response.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert FakeAsyncClient.last_request["url"] == "http://upstream:8080/v1/chat/completions"
    assert FakeAsyncClient.last_request["json"]["tool_choice"] == "required"
    assert FakeAsyncClient.last_request["json"]["max_tokens"] == 32768



def test_passthrough_endpoint_forwards_requests(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    app = create_app(Settings(log_dir=tmp_path, mode="balanced", upstream="http://upstream:8080"))
    client = TestClient(app)

    response = client.get("/v1/models")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert FakeAsyncClient.last_request["url"] == "http://upstream:8080/v1/models"
