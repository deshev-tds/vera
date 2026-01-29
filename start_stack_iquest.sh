#!/usr/bin/env bash
set -euo pipefail

# iquest-style model defaults
export PROMPT_PROFILE="${PROMPT_PROFILE:-iquest}"
export SYSTEM_ROLE="${SYSTEM_ROLE:-user}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$ROOT_DIR/start_stack.sh"
