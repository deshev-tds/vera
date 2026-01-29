#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

STACK_DIR="${STACK_DIR:-.stack}"
DASH_PID_FILE="$STACK_DIR/dashboard.pid"

echo "[1/2] Stopping dashboard..."
if [[ -f "$DASH_PID_FILE" ]]; then
  PID="$(cat "$DASH_PID_FILE" || true)"
  if [[ -n "${PID:-}" ]] && kill -0 "$PID" >/dev/null 2>&1; then
    kill "$PID" || true
    for _ in {1..20}; do
      if kill -0 "$PID" >/dev/null 2>&1; then
        sleep 0.1
      else
        break
      fi
    done
    if kill -0 "$PID" >/dev/null 2>&1; then
      echo "Dashboard did not exit; sending SIGKILL..."
      kill -9 "$PID" || true
    fi
    echo "Stopped dashboard pid=$PID"
  else
    echo "Dashboard not running."
  fi
  rm -f "$DASH_PID_FILE"
else
  echo "No pid file found."
fi

echo "[2/2] Done."
