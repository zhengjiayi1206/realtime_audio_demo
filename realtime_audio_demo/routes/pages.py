from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from realtime_audio_demo.config import (
    DEFAULT_CHAT_PROMPT,
    DEFAULT_FINAL_PROMPT,
    MAX_HISTORY_TURNS,
    PREFILL_MODE,
    QWEN_API_BASE,
    QWEN_MODALITIES,
    QWEN_MODEL,
    QWEN_SPEAKER,
    REALTIME_DEFAULT_SKILLS,
    SILERO_VAD_ENABLED,
    SILERO_VAD_MAX_SPEECH_MS,
    SILERO_VAD_MIN_SILENCE_MS,
    SILERO_VAD_MIN_SPEECH_MS,
    SILERO_VAD_THRESHOLD,
    STATIC_DIR,
    STREAM_FINAL_OUTPUT,
    TARGET_SAMPLE_RATE,
    resolved_provider,
)
from realtime_audio_demo.services.skill_loader import list_runtime_skills


router = APIRouter()
NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
}


def static_page(name: str) -> FileResponse:
    return FileResponse(STATIC_DIR / name, headers=NO_CACHE_HEADERS)


@router.get("/")
async def index() -> FileResponse:
    return static_page("index.html")


@router.get("/demo")
async def demo() -> FileResponse:
    return static_page("demo.html")


@router.get("/chatbox")
async def chatbox() -> FileResponse:
    return static_page("chatbox.html")


@router.get("/chat")
async def chat() -> FileResponse:
    return static_page("chat.html")


@router.get("/realtime")
async def realtime() -> RedirectResponse:
    return RedirectResponse("/chatbox")


@router.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "ok": True,
            "qwen_api_base": QWEN_API_BASE,
            "model": QWEN_MODEL,
            "prefill_mode": PREFILL_MODE,
            "provider": resolved_provider(),
            "modalities": QWEN_MODALITIES,
            "speaker": QWEN_SPEAKER or None,
            "target_sample_rate": TARGET_SAMPLE_RATE,
            "max_history_turns": MAX_HISTORY_TURNS,
            "stream_final_output": STREAM_FINAL_OUTPUT,
            "silero_vad": {
                "enabled": SILERO_VAD_ENABLED,
                "threshold": SILERO_VAD_THRESHOLD,
                "min_speech_ms": SILERO_VAD_MIN_SPEECH_MS,
                "min_silence_ms": SILERO_VAD_MIN_SILENCE_MS,
                "max_speech_ms": SILERO_VAD_MAX_SPEECH_MS,
            },
            "realtime_default_skills": REALTIME_DEFAULT_SKILLS,
            "default_prompt": DEFAULT_FINAL_PROMPT,
            "default_chat_prompt": DEFAULT_CHAT_PROMPT,
        }
    )


@router.get("/api/realtime/skills")
@router.get("/api/chatbox/skills")
async def chatbox_skills() -> JSONResponse:
    return JSONResponse({"skills": list_runtime_skills(), "default_skills": REALTIME_DEFAULT_SKILLS})
