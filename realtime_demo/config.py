import os
from pathlib import Path

from full_duplex_demo.config import QWEN_API_BASE, QWEN_MODEL

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

QWEN_REALTIME_MODEL = os.getenv("QWEN_REALTIME_MODEL", QWEN_MODEL)
QWEN_REALTIME_API_KEY = os.getenv("QWEN_REALTIME_API_KEY", os.getenv("QWEN_API_KEY", ""))
QWEN_REALTIME_VOICE = os.getenv("QWEN_REALTIME_VOICE", "")
QWEN_REALTIME_SAMPLE_RATE = int(os.getenv("QWEN_REALTIME_SAMPLE_RATE", "16000"))
QWEN_REALTIME_SEND_RESPONSE_CREATE = os.getenv("QWEN_REALTIME_SEND_RESPONSE_CREATE", "0") == "1"


def realtime_ws_url() -> str:
    configured = os.getenv("QWEN_REALTIME_WS", "").strip()
    if configured:
        return configured

    base = QWEN_API_BASE.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    return f"{base}/realtime"
