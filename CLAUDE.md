# CLAUDE.md

This repository contains `antiloop-proxy`, a generic OpenAI-compatible proxy for local tool-calling models.

## What Claude Code should optimize for

- Keep the project generic.
- Do not add user-specific paths, usernames, hostnames, tokens, or infrastructure references.
- Preserve OpenAI-compatible HTTP semantics.
- Prefer environment variables over hardcoded configuration.
- Keep anti-loop behavior explainable and covered by tests.

## Primary deployment target

Assume `antiloop-proxy` is deployed **beside** a local model server, not inside the client application.

Typical topology:

- upstream model server on `127.0.0.1:8080`
- `antiloop-proxy` on `127.0.0.1:8081`
- client base URL set to `http://127.0.0.1:8081/v1`

A recommended pairing for this repository is `qwen3-coder-next` running locally, including IQ3 variants. Start with proxy mode `balanced` for that pairing.

## Standard install flow

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

Recommended runtime settings:

```bash
export PROXY_UPSTREAM=http://127.0.0.1:8080
export PROXY_MODE=balanced
export PROXY_PORT=8081
export PROXY_MAX_TOKENS_OVERRIDE=32768
export PROXY_LOOP_MESSAGE_LOOKBACK=10
export PROXY_LOOP_MIN_TOOL_RESULTS=3
export PROXY_LOOP_MIN_COMMON_TOKENS=2
```

Start the proxy:

```bash
python -m antiloop_proxy
```

Or:

```bash
./run.sh
```

## Before changing code

1. Read `README.md` for the public operator-facing contract.
2. Read `AGENTS.md` for deployment expectations shared across agents.
3. Preserve backward compatibility of `main.py` as a thin wrapper.

## Validation steps

After making changes:

```bash
python -m pytest
python -m py_compile main.py antiloop_proxy/*.py tests/test_proxy.py
```

For deployment validation:

1. `curl http://127.0.0.1:8081/healthz`
2. `curl http://127.0.0.1:8081/configz`
3. send one normal request through `/v1/chat/completions`
4. send one tool-calling request through `/v1/chat/completions`
5. inspect `logs/*.log` and `logs/*.jsonl`

## When to use `strict`

Only switch from `balanced` to `strict` if the upstream model repeatedly stops right after tool results and needs more forceful continuation.
