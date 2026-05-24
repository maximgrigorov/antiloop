# antiloop-proxy

`antiloop-proxy` is a small OpenAI-compatible HTTP proxy designed for local or perimeter-contained LLM deployments.

It sits between a client application and an upstream model server such as `llama.cpp`, logs request/response metadata, detects repeated tool-result loops, and can selectively modify requests to keep tool-calling sessions moving.

## Features

- OpenAI-compatible `/v1/chat/completions` proxy
- Transparent passthrough for other endpoints such as `/v1/models`
- Anti-loop heuristics for repeated tool-result cycles
- Three policy modes:
  - `observe` — log only, never mutate requests
  - `balanced` — inject `tool_choice=required` after tool results unless a loop is detected
  - `strict` — force `tool_choice=required` after tool results unless a loop is detected
- Daily `.log` and `.jsonl` logs
- Redaction of common secrets in log previews
- Simple health and config endpoints
- User-service-friendly `systemd` unit

## Architecture

Client application -> `antiloop-proxy` -> upstream OpenAI-compatible model server

The proxy focuses on two operational problems common with local tool-calling models:

1. the model stops after receiving a tool result even though it should continue;
2. the model falls into repetitive tool-result loops.

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/<your-namespace>/antiloop.git
cd antiloop
```

### 2. Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
```

### 3. Create your runtime environment file

```bash
cp .env.example .env
```

`antiloop-proxy` does **not** auto-load `.env`, so load it explicitly before starting:

```bash
set -a
source .env
set +a
python -m antiloop_proxy
```

Or use the helper script after exporting the same variables in your shell.


## Running next to a local model server

Example with `llama.cpp`:

```bash
# terminal 1: upstream model server
llama-server \
  --host 127.0.0.1 \
  --port 8080 \
  --jinja \
  --ctx-size 65536 \
  --model /models/model.gguf

# terminal 2: proxy
export PROXY_UPSTREAM=http://127.0.0.1:8080
export PROXY_PORT=8081
export PROXY_MODE=balanced
python -m antiloop_proxy
```

Then point the client application at:

```text
http://127.0.0.1:8081/v1
```

## Recommended pairing: `qwen3-coder-next` (IQ3)

This project is a good fit when the upstream model is `qwen3-coder-next`, including lower-bit local deployments such as IQ3, and you want an external proxy layer to stabilize tool-calling behavior.

A practical `llama.cpp` pairing looks like this:

```bash
# terminal 1: llama.cpp server with a qwen3-coder-next IQ3 GGUF
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

Then start the proxy with the bundled example configuration:

```bash
cp .env.example .env
set -a
source .env
set +a
python -m antiloop_proxy
```

Important notes for this pairing:

- start with `PROXY_MODE=balanced`
- keep `PROXY_FORCE_TOOL_CHOICE_WHEN_TOOL_LAST=true`
- use `PROXY_MAX_TOKENS_OVERRIDE=32768` as a conservative default unless your client sets `max_tokens`
- inspect `logs/*.jsonl` before moving to `strict`
- if your `llama.cpp` build or hardware cannot sustain a very large context, reduce `--ctx-size` first rather than immediately changing proxy policy

Start with `balanced`, inspect logs, and only move to `strict` if the model frequently stops after tool results instead of continuing the chain.

The repository also includes ready-to-use deployment artifacts for this pairing:

- `compose.yaml` — Docker Compose example for `llama.cpp` + `antiloop-proxy`
- `examples/launch-qwen3-coder-next-llamacpp.sh` — sample `llama-server` launch wrapper
- `antiloop-proxy.envfile.service` — systemd user unit that reads `%h/antiloop/.env`

## Modes

### `observe`
- No request mutation
- Best for initial diagnostics and baseline logging

### `balanced`
- Default
- Injects `tool_choice=required` only when the last message is a tool result and no probable loop is detected
- Preserves explicit non-auto client choices

### `strict`
- More aggressive
- Forces `tool_choice=required` after tool results unless a probable loop is detected
- Useful when a local model frequently stops instead of continuing the tool chain

## Configuration

All behavior is configured through environment variables.

### Core settings

- `PROXY_UPSTREAM` — upstream OpenAI-compatible endpoint base URL
- `PROXY_HOST` — listen host, default `0.0.0.0`
- `PROXY_PORT` — listen port, default `8081`
- `PROXY_MODE` — `observe`, `balanced`, or `strict`
- `PROXY_LOG_DIR` — log directory, default `~/antiloop/logs`

### Request handling

- `PROXY_MAX_TOKENS_OVERRIDE` — default `32768`; only applied when the client request omits `max_tokens`
- `PROXY_REQUEST_TIMEOUT` — upstream timeout for chat completions
- `PROXY_PASSTHROUGH_TIMEOUT` — timeout for non-chat passthrough endpoints
- `PROXY_FORCE_TOOL_CHOICE_WHEN_TOOL_LAST` — default `true`

### Anti-loop tuning

- `PROXY_LOOP_MESSAGE_LOOKBACK` — recent messages to inspect, default `10`
- `PROXY_LOOP_MIN_TOOL_RESULTS` — minimum tool results before loop detection can trigger, default `3`
- `PROXY_LOOP_MIN_COMMON_TOKENS` — minimum common significant tokens, default `2`
- `PROXY_LOOP_SIMILARITY_RATIO` — similar-length threshold, default `1.25`

### Logging and redaction

- `PROXY_REDACT_LOGS` — default `true`
- `PROXY_PREVIEW_LENGTH` — preview length used in human and JSONL logs

## Endpoints

- `GET /healthz` — health summary
- `GET /configz` — resolved runtime config
- `POST /v1/chat/completions` — policy-aware proxy
- `* /{path}` — generic passthrough for all other endpoints

## Logging

Logs are written daily into the configured log directory:

- `YYYY-MM-DD.log` — human-readable summaries
- `YYYY-MM-DD.jsonl` — structured records for analysis tools

Only preview fragments are logged, and common credentials/tokens are redacted by default.

## Testing

Run the test suite:

```bash
python -m pytest
```

## Docker Compose example

A ready example is included as `compose.yaml`.

It runs:

- `llama-server` on port `8080`
- `antiloop-proxy` on port `8081`

Typical flow:

```bash
cp .env.example .env
docker compose up -d
```

Then point the client at:

```text
http://127.0.0.1:8081/v1
```

Adjust the model mount and GGUF filename in `compose.yaml` for your host.

## Sample launch wrapper for llama.cpp

A shell example is included at:

```text
examples/launch-qwen3-coder-next-llamacpp.sh
```

Example:

```bash
chmod +x examples/launch-qwen3-coder-next-llamacpp.sh
MODEL_PATH=/models/qwen3-coder-next-iq3.gguf examples/launch-qwen3-coder-next-llamacpp.sh
```

## systemd user service

Two sample user-service units are included:

- `antiloop-proxy.service` — inline `Environment=` values
- `antiloop-proxy.envfile.service` — `EnvironmentFile=%h/antiloop/.env`

### Inline environment variant

```bash
mkdir -p ~/.config/systemd/user
cp antiloop-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now antiloop-proxy.service
systemctl --user status antiloop-proxy.service
```

### `.env` / `EnvironmentFile=` variant

```bash
cp .env.example .env
mkdir -p ~/.config/systemd/user
cp antiloop-proxy.envfile.service ~/.config/systemd/user/antiloop-proxy.service
systemctl --user daemon-reload
systemctl --user enable --now antiloop-proxy.service
systemctl --user status antiloop-proxy.service
```

This variant is useful when you want systemd to read the same simple `KEY=VALUE` file that you edit manually.

## When to use this project

This project is useful when:

- the client must stay pointed at an OpenAI-compatible API;
- the upstream model is local or perimeter-contained;
- tool-calling reliability matters more than raw benchmark quality;
- you need logs for repeated tool-result failures or local-model stalls.

## Similar tools / related projects

There are broader proxy and guardrail projects in this space, for example:

- `antoinezambelli/forge` — a more feature-rich framework and proxy for local tool-calling workflows
- `BerriAI/litellm` — a general AI gateway/proxy, broader in scope but not focused on this exact anti-loop problem
- `vibheksoni/UniClaudeProxy` — protocol-translation proxy for coding tools

`antiloop-proxy` intentionally stays small and focused on OpenAI-compatible local deployments with tool-loop diagnostics.
