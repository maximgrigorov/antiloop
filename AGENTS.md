# AGENTS.md

This repository contains `antiloop-proxy`, a generic OpenAI-compatible proxy service for local tool-calling models.

## Purpose

Use this service when a client application must talk to an OpenAI-compatible endpoint, but the operator wants a local proxy layer that can:

- log request/response summaries,
- detect repeated tool-result loops,
- selectively inject `tool_choice=required`,
- preserve perimeter-contained model traffic.

## Deployment model

Install this service **next to** the local model server, not inside the client application.

Typical layout on a Linux host:

```text
/opt/local-llm/
  model-server/
  antiloop/
```

Example:

- upstream model server listens on `127.0.0.1:8080`
- `antiloop-proxy` listens on `127.0.0.1:8081`
- client application points to `http://127.0.0.1:8081/v1`

## Recommended install procedure

### 1. Place the repository beside the model server

```bash
cd /opt/local-llm
git clone <repo-url> antiloop
cd antiloop
```

If an autonomous coding agent is doing the deployment, let it work from the repository root so it can read this file before making changes. For Claude Code specifically, keep a `CLAUDE.md` file at the repository root and have the agent follow it together with this `AGENTS.md`.

### 2. Create the environment and install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

### 3. Configure runtime settings

Copy the bundled example file and edit it for the target host:

```bash
cp .env.example .env
```

Then load it explicitly before starting the proxy:

```bash
set -a
source .env
set +a
```

Minimal starting values:

```bash
export PROXY_UPSTREAM=http://127.0.0.1:8080
export PROXY_MODE=balanced
export PROXY_PORT=8081
```

Recommended tuning for `qwen3-coder-next` IQ3 style deployments:

```bash
export PROXY_MAX_TOKENS_OVERRIDE=32768
export PROXY_LOOP_MESSAGE_LOOKBACK=10
export PROXY_LOOP_MIN_TOOL_RESULTS=3
export PROXY_LOOP_MIN_COMMON_TOKENS=2
export PROXY_FORCE_TOOL_CHOICE_WHEN_TOOL_LAST=true
```

### 4. Start the service

```bash
python -m antiloop_proxy
```

Or:

```bash
./run.sh
```

For this repository's main target pairing, also see:

- `examples/launch-qwen3-coder-next-llamacpp.sh`
- `compose.yaml`
- `antiloop-proxy.envfile.service`

## systemd user service setup

Two user-service templates are provided:

- `antiloop-proxy.service` — inline `Environment=` values
- `antiloop-proxy.envfile.service` — reads `%h/antiloop/.env` via `EnvironmentFile=`

Inline variant:

```bash
mkdir -p ~/.config/systemd/user
cp antiloop-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now antiloop-proxy.service
```

`.env` variant:

```bash
cp .env.example .env
mkdir -p ~/.config/systemd/user
cp antiloop-proxy.envfile.service ~/.config/systemd/user/antiloop-proxy.service
systemctl --user daemon-reload
systemctl --user enable --now antiloop-proxy.service
```

If the repository lives somewhere other than `%h/antiloop`, update:

- `WorkingDirectory=`
- `ExecStart=`
- `EnvironmentFile=` or inline `Environment=` values

## Operator guidance

### Start with `balanced`

Use `balanced` first. It is the safest default for most local tool-calling models.

### Use `observe` before aggressive tuning

If the client behavior is unknown, first run in `observe` mode to collect logs without mutating requests.

### Use `strict` only when needed

Switch to `strict` when the upstream model repeatedly stops after tool results and you want the proxy to force continuation more aggressively.

## Validation checklist

After installation:

1. `curl http://127.0.0.1:8081/healthz`
2. `curl http://127.0.0.1:8081/configz`
3. send one normal non-tool request through `/v1/chat/completions`
4. send one tool-calling request through `/v1/chat/completions`
5. inspect `logs/*.log` and `logs/*.jsonl`

## Development workflow

### Run tests

```bash
python -m pytest
```

### Local smoke test

```bash
PROXY_UPSTREAM=http://127.0.0.1:8080 PROXY_MODE=balanced python -m antiloop_proxy
```

### Backward-compatible entrypoint

`main.py` remains as a thin wrapper around `python -m antiloop_proxy`.

## Constraints

- Keep this project generic; do not add host-specific usernames, paths, or infrastructure references.
- Preserve OpenAI-compatible HTTP semantics.
- Prefer environment variables over hardcoded configuration.
- Keep anti-loop logic explainable and test-covered.
