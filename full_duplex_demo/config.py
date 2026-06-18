import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

QWEN_API_BASE = os.getenv("QWEN_API_BASE", "http://127.0.0.1:5440/v1").rstrip("/")
QWEN_MODEL = os.getenv("QWEN_MODEL", "Qwen3-Omni-30B-A3B-Instruct")
QWEN_PROVIDER = os.getenv("QWEN_PROVIDER", "vllm_omni")
QWEN_SPEAKER = os.getenv("QWEN_SPEAKER", "")
TARGET_SAMPLE_RATE = int(os.getenv("TARGET_SAMPLE_RATE", "16000"))
REQUEST_TIMEOUT = float(os.getenv("QWEN_REQUEST_TIMEOUT", "60"))
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "55786"))

# Chunk config
CHUNK_DURATION_S = float(os.getenv("CHUNK_DURATION_S", "1.0"))
ROLLING_AUDIO_CONTEXT_S = float(os.getenv("ROLLING_AUDIO_CONTEXT_S", "4.0"))
TEXT_HISTORY_TURNS = int(os.getenv("TEXT_HISTORY_TURNS", "120"))
MAX_RESPONSE_CHARS = int(os.getenv("MAX_RESPONSE_CHARS", "10"))

_prompt_path = APP_DIR / "prompt.txt"
if _prompt_path.exists():
    SYSTEM_PROMPT = _prompt_path.read_text(encoding="utf-8").strip()
else:
    SYSTEM_PROMPT = "你正在进行实时语音对话。每秒你会听到1秒的用户语音。请像真人聊天一样自然回应，每次都给出具体回应，不要输出占位符。"

_listen_speak_prompt_path = APP_DIR / "prompt_listen_speak.txt"
if _listen_speak_prompt_path.exists():
    LISTEN_SPEAK_PROMPT = _listen_speak_prompt_path.read_text(encoding="utf-8").strip()
else:
    LISTEN_SPEAK_PROMPT = "判断用户是否已经说完话。只输出 speak 或 listen。"


def resolved_provider() -> str:
    if QWEN_PROVIDER != "auto":
        return QWEN_PROVIDER
    if ":5440" in QWEN_API_BASE or ":8091" in QWEN_API_BASE:
        return "vllm_omni"
    return "ms_swift"
