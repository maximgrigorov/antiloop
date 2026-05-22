#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="${VENV:-$DIR/.venv}"
LOGS="${PROXY_LOG_DIR:-$DIR/logs}"
UPSTREAM="${PROXY_UPSTREAM:-http://127.0.0.1:8080}"
HOST="${PROXY_HOST:-0.0.0.0}"
PORT="${PROXY_PORT:-8081}"
MODE="${PROXY_MODE:-balanced}"

mkdir -p "$LOGS"

if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment in $VENV ..."
    python3 -m venv "$VENV"
fi

"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -e "$DIR"

export PROXY_LOG_DIR="$LOGS"
export PROXY_UPSTREAM="$UPSTREAM"
export PROXY_HOST="$HOST"
export PROXY_PORT="$PORT"
export PROXY_MODE="$MODE"

cat <<EOF
Starting antiloop-proxy...
  dir:      $DIR
  venv:     $VENV
  logs:     $LOGS
  upstream: $UPSTREAM
  listen:   $HOST:$PORT
  mode:     $MODE
EOF

exec "$VENV/bin/python" -m antiloop_proxy
