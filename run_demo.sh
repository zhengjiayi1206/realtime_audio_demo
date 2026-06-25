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
export MAX_HISTORY_TURNS="${MAX_HISTORY_TURNS:-10}"
export STREAM_FINAL_OUTPUT="${STREAM_FINAL_OUTPUT:-1}"
export EASYTURN_ENABLED="${EASYTURN_ENABLED:-1}"
export EASYTURN_PRELOAD="${EASYTURN_PRELOAD:-1}"
export EASYTURN_LLM_PATH="${EASYTURN_LLM_PATH:-/path/to/Qwen2.5-0.5B-Instruct}"
export EASYTURN_CHECKPOINT="${EASYTURN_CHECKPOINT:-/path/to/checkpoint.pt}"
export EASYTURN_CONFIG="${EASYTURN_CONFIG:-${SCRIPT_DIR}/easy_turn/config.yaml}"
export EASYTURN_GPU="${EASYTURN_GPU:-0}"
export EASYTURN_ACK_TEXT="${EASYTURN_ACK_TEXT:-嗯，我在听，你继续。}"
export EASYTURN_MAX_AUDIO_SECONDS="${EASYTURN_MAX_AUDIO_SECONDS:-30}"
export RUNTIME_SKILLS_DIR="${RUNTIME_SKILLS_DIR:-${SCRIPT_DIR}/runtime_skills}"
export REALTIME_DEFAULT_SKILLS="${REALTIME_DEFAULT_SKILLS:-}"
export REALTIME_SKILL_MAX_CHARS="${REALTIME_SKILL_MAX_CHARS:-12000}"
# Full-duplex chunk config
export CHUNK_DURATION_S="${CHUNK_DURATION_S:-1.0}"
export ROLLING_AUDIO_CONTEXT_S="${ROLLING_AUDIO_CONTEXT_S:-4.0}"
export TEXT_HISTORY_TURNS="${TEXT_HISTORY_TURNS:-120}"
export MAX_RESPONSE_CHARS="${MAX_RESPONSE_CHARS:-10}"
export HOST="${HOST:-0.0.0.0}"
export PORT="${PORT:-55785}"

cd "${SCRIPT_DIR}"
exec "${VENV_PY}" -m uvicorn realtime_demo.app:app --host "${HOST}" --port "${PORT}" --log-level info
