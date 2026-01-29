#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8844}"
WORK_DIR_REL="${WORK_DIR:-./work/first-run}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ -x ".venv/bin/python" ]]; then
  PYTHON_BIN=".venv/bin/python"
fi

MAX_STEPS="${MAX_STEPS:-}"
if [[ -z "${MAX_STEPS}" ]]; then
  if [[ -t 0 ]]; then
    echo "Agent step budget:"
    echo "  1) unlimited"
    echo "  2) set integer (default 120)"
    read -r -p "Choose [1/2]: " choice
    case "${choice:-2}" in
      1)
        MAX_STEPS="0"
        ;;
      2)
        read -r -p "Max steps (integer, default 120): " steps
        steps="${steps:-120}"
        if [[ "${steps}" =~ ^[0-9]+$ ]]; then
          MAX_STEPS="${steps}"
        else
          echo "Invalid number, defaulting to 120."
          MAX_STEPS="120"
        fi
        ;;
      *)
        MAX_STEPS="120"
        ;;
    esac
  else
    MAX_STEPS="120"
  fi
fi
export MAX_STEPS

WORK_DIR_ABS="$("$PYTHON_BIN" -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$WORK_DIR_REL")"

STACK_DIR="${STACK_DIR:-.stack}"
mkdir -p "$STACK_DIR"
mkdir -p "$WORK_DIR_ABS"

echo "[1/5] Checking Python dependencies..."
if ! "$PYTHON_BIN" - <<'PY'
import sys
missing = []
for mod in ("docker","requests"):
    try:
        __import__(mod)
    except Exception:
        missing.append(mod)
if missing:
    print("Missing Python deps:", ", ".join(missing))
    sys.exit(2)
print("OK")
PY
then
  if [[ "${AUTO_INSTALL:-0}" == "1" ]]; then
    echo "AUTO_INSTALL=1: creating venv + installing requirements..."
    if [[ ! -d ".venv" ]]; then
      python3 -m venv .venv
    fi
    .venv/bin/pip install -r requirements.txt
    PYTHON_BIN=".venv/bin/python"
    "$PYTHON_BIN" -c 'import docker,requests; print("OK")' >/dev/null
    echo "OK"
  else
    echo "Fix: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    echo "Or run: AUTO_INSTALL=1 bash start_stack.sh"
    exit 2
  fi
fi

echo "[2/5] Checking Docker daemon..."
if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker CLI not found. Install Docker Desktop / Docker Engine."
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon not reachable. Is Docker running?"
  exit 2
fi
echo "OK"

echo "[3/5] Ensuring sandbox image exists (build if needed)..."
"$PYTHON_BIN" run.py build >/dev/null
echo "OK"

echo "[4/5] Starting dashboard..."
DASH_LOG="$STACK_DIR/dashboard.log"
DASH_PID_FILE="$STACK_DIR/dashboard.pid"
DASH_STARTED=0

cleanup() {
  status=$?
  if [[ $status -ne 0 && "${DASH_STARTED}" == "1" ]]; then
    echo "Startup failed; stopping dashboard..."
    if [[ -f "$DASH_PID_FILE" ]]; then
      pid="$(cat "$DASH_PID_FILE" || true)"
      if [[ -n "${pid:-}" ]] && kill -0 "$pid" >/dev/null 2>&1; then
        kill "$pid" || true
      fi
      rm -f "$DASH_PID_FILE"
    fi
  fi
}
trap cleanup EXIT

if [[ -f "$DASH_PID_FILE" ]] && kill -0 "$(cat "$DASH_PID_FILE")" >/dev/null 2>&1; then
  echo "Dashboard already running (pid=$(cat "$DASH_PID_FILE"))."
else
  rm -f "$DASH_PID_FILE"
  nohup "$PYTHON_BIN" run.py dashboard --base-dir . --host "$HOST" --port "$PORT" >"$DASH_LOG" 2>&1 &
  echo $! > "$DASH_PID_FILE"
  DASH_STARTED=1
  echo "Dashboard pid=$(cat "$DASH_PID_FILE") (log=$DASH_LOG)"
fi

DASH_URL="http://${HOST}:${PORT}/?work_dir=${WORK_DIR_REL}"
METRICS_URL="http://${HOST}:${PORT}/metrics?work_dir=${WORK_DIR_REL}"

echo "[5/5] Health checks (dashboard + sandbox container)..."

echo "Checking dashboard /metrics..."
ok_metrics=0
for _ in {1..40}; do
  if curl -fsS --max-time 1 "$METRICS_URL" 2>/dev/null | grep -q "^dra_events_total"; then
    echo "OK"
    ok_metrics=1
    break
  fi
  sleep 0.25
done
if [[ "$ok_metrics" != "1" ]]; then
  echo "ERROR: dashboard /metrics did not become ready. See $DASH_LOG"
  exit 3
fi

echo "Checking we can start/exec/stop a sandbox container..."
WORK_DIR_ABS="$WORK_DIR_ABS" "$PYTHON_BIN" - <<'PY'
import os
from agent.tools import SandboxManager

work_dir = os.environ["WORK_DIR_ABS"]
sm = SandboxManager()
s = sm.start(input_dir=None, work_dir=work_dir, network_enabled=True)
try:
    code, out = sm.exec(s, ["bash","-lc","python3 --version && rg --version && echo SANDBOX_OK"], timeout_s=20)
    print(out.strip())
    if code != 0 or "SANDBOX_OK" not in out:
        raise SystemExit(3)
finally:
    sm.stop(s)
print("OK")
PY

echo
echo "READY"
echo "Dashboard: $DASH_URL"
echo "Metrics:   $METRICS_URL"
