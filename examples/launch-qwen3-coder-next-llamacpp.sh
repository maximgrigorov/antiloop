#!/usr/bin/env bash
set -euo pipefail

MODEL_PATH="${MODEL_PATH:-/models/qwen3-coder-next-iq3.gguf}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"
CTX_SIZE="${CTX_SIZE:-262144}"
THREADS="${THREADS:-12}"
PARALLEL="${PARALLEL:-1}"

exec llama-server \
  --host "$HOST" \
  --port "$PORT" \
  --jinja \
  --ctx-size "$CTX_SIZE" \
  --parallel "$PARALLEL" \
  --threads "$THREADS" \
  --flash-attn \
  --model "$MODEL_PATH"
