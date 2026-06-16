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
SILERO_VAD_ENABLED = os.getenv("SILERO_VAD_ENABLED", "1").lower() not in {"0", "false", "off", "no"}
SILERO_VAD_PRELOAD = os.getenv("SILERO_VAD_PRELOAD", "1").lower() not in {"0", "false", "off", "no"}
SILERO_VAD_ONNX = os.getenv("SILERO_VAD_ONNX", "0").lower() in {"1", "true", "on", "yes"}
SILERO_VAD_THRESHOLD = float(os.getenv("SILERO_VAD_THRESHOLD", "0.5"))
SILERO_VAD_MIN_SPEECH_MS = int(os.getenv("SILERO_VAD_MIN_SPEECH_MS", "180"))
SILERO_VAD_MIN_SILENCE_MS = int(os.getenv("SILERO_VAD_MIN_SILENCE_MS", "450"))
SILERO_VAD_MAX_SPEECH_MS = int(os.getenv("SILERO_VAD_MAX_SPEECH_MS", "30000"))
SILERO_VAD_SPEECH_PAD_MS = int(os.getenv("SILERO_VAD_SPEECH_PAD_MS", "30"))
REALTIME_DEFAULT_SKILLS = [
    item.strip() for item in os.getenv("REALTIME_DEFAULT_SKILLS", "intent-recognition").split(",") if item.strip()
]
REALTIME_SKILL_MAX_CHARS = int(os.getenv("REALTIME_SKILL_MAX_CHARS", "12000"))
SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))
DEFAULT_FINAL_PROMPT = (
    "你正在和用户进行实时语音对话。请直接理解用户想问什么，不要转写、复述或翻译语音内容。"
    "回答要像真人聊天一样自然、口语化、简短，优先用一两句话说清楚。"
    "只有问题确实复杂时再分点说明；不要啰嗦，不要输出无关解释。"
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
