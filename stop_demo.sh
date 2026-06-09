#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="${PID_FILE:-${SCRIPT_DIR}/qwen3_omni_demo.pid}"

if [[ ! -f "${PID_FILE}" ]]; then
  echo "PID file not found: ${PID_FILE}"
  exit 0
fi

pid="$(cat "${PID_FILE}")"
if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
  kill "${pid}"
  echo "Stopped demo backend: pid=${pid}"
else
  echo "Process is not running: pid=${pid}"
fi

rm -f "${PID_FILE}"
