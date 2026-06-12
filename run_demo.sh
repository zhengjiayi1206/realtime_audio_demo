#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PY="${SCRIPT_DIR}/.venv/bin/python"

if [[ ! -x "${VENV_PY}" ]]; then
  echo "Cannot find project venv python: ${VENV_PY}" >&2
  echo "Run 'uv sync' in ${SCRIPT_DIR} first." >&2
  exit 1
fi

export QWEN_API_BASE="${QWEN_API_BASE:-http://127.0.0.1:5440/v1}"
export QWEN_MODEL="${QWEN_MODEL:-Qwen3-Omni-30B-A3B-Instruct}"
export QWEN_PROVIDER="${QWEN_PROVIDER:-vllm_omni}"
export PREFILL_INTERVAL_MS="${PREFILL_INTERVAL_MS:-600}"
export PREFILL_MODE="${PREFILL_MODE:-cumulative_probe}"
export TARGET_SAMPLE_RATE="${TARGET_SAMPLE_RATE:-16000}"
export FINAL_MAX_TOKENS="${FINAL_MAX_TOKENS:-512}"
export STREAM_FINAL_OUTPUT="${STREAM_FINAL_OUTPUT:-1}"
export RUNTIME_SKILLS_DIR="${RUNTIME_SKILLS_DIR:-${SCRIPT_DIR}/runtime_skills}"
export REALTIME_DEFAULT_SKILLS="${REALTIME_DEFAULT_SKILLS:-}"
export REALTIME_SKILL_MAX_CHARS="${REALTIME_SKILL_MAX_CHARS:-12000}"

cd "${SCRIPT_DIR}"
exec "${VENV_PY}" -m uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-55785}" --log-level info
