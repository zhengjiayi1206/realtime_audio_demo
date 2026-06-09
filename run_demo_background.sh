#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${LOG_FILE:-${SCRIPT_DIR}/qwen3_omni_demo.log}"
PID_FILE="${PID_FILE:-${SCRIPT_DIR}/qwen3_omni_demo.pid}"

export UV_CACHE_DIR="${UV_CACHE_DIR:-${TMPDIR:-/tmp}/uv-cache-${USER}}"
export QWEN_API_BASE="${QWEN_API_BASE:-http://127.0.0.1:5440/v1}"
export QWEN_MODEL="${QWEN_MODEL:-Qwen3-Omni-30B-A3B-Instruct}"
export QWEN_PROVIDER="${QWEN_PROVIDER:-vllm_omni}"
export PREFILL_INTERVAL_MS="${PREFILL_INTERVAL_MS:-600}"
export PREFILL_MODE="${PREFILL_MODE:-cumulative_probe}"
export TARGET_SAMPLE_RATE="${TARGET_SAMPLE_RATE:-16000}"
export FINAL_MAX_TOKENS="${FINAL_MAX_TOKENS:-512}"
export MAX_HISTORY_TURNS="${MAX_HISTORY_TURNS:-10}"
export STREAM_FINAL_OUTPUT="${STREAM_FINAL_OUTPUT:-1}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-55785}"

cd "${SCRIPT_DIR}"

if [[ -f "${PID_FILE}" ]]; then
  old_pid="$(cat "${PID_FILE}")"
  if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "Demo backend is already running: pid=${old_pid}"
    echo "Log: ${LOG_FILE}"
    exit 0
  fi
fi

nohup uv run uvicorn app:app --host "${HOST}" --port "${PORT}" > "${LOG_FILE}" 2>&1 &
pid="$!"
echo "${pid}" > "${PID_FILE}"

echo "Started demo backend: pid=${pid}"
echo "URL: http://127.0.0.1:${PORT}"
echo "Log: ${LOG_FILE}"
echo "Stop: kill \$(cat ${PID_FILE})"
