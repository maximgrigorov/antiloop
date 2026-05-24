# CLAUDE.md

This repository contains `antiloop-proxy`, a generic OpenAI-compatible proxy for local tool-calling models.

## What Claude Code should optimize for

- Keep the project generic.
- Do not add user-specific paths, usernames, hostnames, tokens, or infrastructure references.
- Preserve OpenAI-compatible HTTP semantics.
- Prefer environment variables over hardcoded configuration.
- Keep anti-loop behavior explainable and covered by tests.
- Preserve the project as a small focused proxy, not a general orchestration framework.

## Primary deployment target

Assume `antiloop-proxy` is deployed **beside** a local `llama.cpp` model server, not inside the client application.

Typical topology:

- upstream `llama-server` on `127.0.0.1:8080`
- `antiloop-proxy` on `127.0.0.1:8081`
- client base URL set to `http://127.0.0.1:8081/v1`

The main expected pairing for this repository is `qwen3-coder-next`, including IQ3 GGUF variants, served by `llama.cpp`.

## Reference deployment flow for qwen3-coder-next + llama.cpp

### 1. Start the upstream model server

Use a command in this shape:

```bash
llama-server \
  --host 127.0.0.1 \
  --port 8080 \
  --jinja \
  --ctx-size 262144 \
  --parallel 1 \
  --threads 12 \
  --flash-attn \
  --model /models/qwen3-coder-next-iq3.gguf
```

Notes for Claude Code:

- keep the upstream endpoint OpenAI-compatible
- prefer changing `--ctx-size`, GPU offload, or model quant before making proxy heuristics more aggressive
- do not hardcode model paths into Python code; keep them in env files, shell commands, or service units

### 2. Install the proxy

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

### 3. Prepare runtime configuration

```bash
cp .env.example .env
set -a
source .env
set +a
```

Important: `.env` is documentation and operator convenience; the proxy does **not** auto-load it internally.

### 4. Start the proxy

```bash
python -m antiloop_proxy
```

Or:

```bash
./run.sh
```

## Recommended starting proxy settings

Assume these values unless a user request or measured behavior suggests otherwise:

```bash
export PROXY_UPSTREAM=http://127.0.0.1:8080
export PROXY_HOST=127.0.0.1
export PROXY_PORT=8081
export PROXY_MODE=balanced
export PROXY_MAX_TOKENS_OVERRIDE=32768
export PROXY_LOOP_MESSAGE_LOOKBACK=10
export PROXY_LOOP_MIN_TOOL_RESULTS=3
export PROXY_LOOP_MIN_COMMON_TOKENS=2
export PROXY_LOOP_SIMILARITY_RATIO=1.25
export PROXY_FORCE_TOOL_CHOICE_WHEN_TOOL_LAST=true
```

## Policy guidance for this pairing

For `qwen3-coder-next` on `llama.cpp`:

- use `balanced` first
- only switch to `strict` after inspecting logs that show repeated post-tool stalls
- leave `PROXY_FORCE_TOOL_CHOICE_WHEN_TOOL_LAST=true` unless there is a concrete regression
- keep `PROXY_MAX_TOKENS_OVERRIDE` moderate unless the client already sets its own `max_tokens`
- prefer evidence from `logs/*.jsonl` over intuition when tuning

## Before changing code

1. Read `README.md` for the public operator-facing contract.
2. Read `AGENTS.md` for deployment expectations shared across agents.
3. Preserve backward compatibility of `main.py` as a thin wrapper.
4. Update docs when changing runtime assumptions, env names, or deployment flow.

## Validation steps

After making changes:

```bash
python -m pytest
python -m py_compile main.py antiloop_proxy/*.py tests/test_proxy.py
```

For deployment validation:

1. verify the upstream `llama-server` is reachable on `127.0.0.1:8080`
2. `curl http://127.0.0.1:8081/healthz`
3. `curl http://127.0.0.1:8081/configz`
4. send one normal request through `/v1/chat/completions`
5. send one tool-calling request through `/v1/chat/completions`
6. inspect `logs/*.log` and `logs/*.jsonl`

## When to use `strict`

Only switch from `balanced` to `strict` if the upstream model repeatedly stops right after tool results and needs more forceful continuation.