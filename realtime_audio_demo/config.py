import os
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
CAPTURE_DIR = APP_DIR / "captures"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_SKILLS_DIR = Path(os.getenv("RUNTIME_SKILLS_DIR", str(APP_DIR / "runtime_skills")))
RUNTIME_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

QWEN_API_BASE = os.getenv("QWEN_API_BASE", "http://127.0.0.1:5440/v1").rstrip("/")
QWEN_MODEL = os.getenv("QWEN_MODEL", "Qwen3-Omni-30B-A3B-Instruct")
QWEN_PROVIDER = os.getenv("QWEN_PROVIDER", "auto")
QWEN_MODALITIES = [
    item.strip() for item in os.getenv("QWEN_MODALITIES", "text,audio").split(",") if item.strip()
]
QWEN_SPEAKER = os.getenv("QWEN_SPEAKER", "")
TARGET_SAMPLE_RATE = int(os.getenv("TARGET_SAMPLE_RATE", "16000"))
DEFAULT_PREFILL_MS = int(os.getenv("PREFILL_INTERVAL_MS", "600"))
PREFILL_MODE = os.getenv("PREFILL_MODE", "cumulative_probe")
FINAL_MAX_TOKENS = int(os.getenv("FINAL_MAX_TOKENS", "512"))
REQUEST_TIMEOUT = float(os.getenv("QWEN_REQUEST_TIMEOUT", "180"))
MAX_HISTORY_TURNS = int(os.getenv("MAX_HISTORY_TURNS", "10"))
STREAM_FINAL_OUTPUT = os.getenv("STREAM_FINAL_OUTPUT", "1").lower() not in {"0", "false", "off", "no"}
REALTIME_DEFAULT_SKILLS = [
    item.strip() for item in os.getenv("REALTIME_DEFAULT_SKILLS", "").split(",") if item.strip()
]
REALTIME_SKILL_MAX_CHARS = int(os.getenv("REALTIME_SKILL_MAX_CHARS", "12000"))
DEFAULT_FINAL_PROMPT = (
    "你正在进行实时语音对话。请不要转写、复述或翻译用户的语音内容。"
    "请先理解用户语音里的意图，然后像聊天助手一样直接回答用户的问题。"
    "请根据问题复杂度给出完整、有帮助的回答；只有在用户只是打招呼时才简短回应。"
)
DEFAULT_CHAT_PROMPT = os.getenv(
    "DEFAULT_CHAT_PROMPT",
    "你是一个通用问答助手。请直接、清楚、准确地回答用户问题。"
    "如果信息不足，先说明缺口，再给出可执行的下一步。",
)


def resolved_provider() -> str:
    if QWEN_PROVIDER != "auto":
        return QWEN_PROVIDER
    if ":5440" in QWEN_API_BASE or ":8091" in QWEN_API_BASE or "vllm-omni" in QWEN_API_BASE:
        return "vllm_omni"
    return "ms_swift"


def normalize_model_name(model: str) -> str:
    if resolved_provider() == "vllm_omni" and "/" not in model:
        return QWEN_MODEL
    return model
