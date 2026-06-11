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
    STATIC_DIR,
    STREAM_FINAL_OUTPUT,
    TARGET_SAMPLE_RATE,
    resolved_provider,
)
from realtime_audio_demo.services.skill_loader import list_runtime_skills


router = APIRouter()


@router.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/demo")
async def demo() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo.html")


@router.get("/chatbox")
async def chatbox() -> FileResponse:
    return FileResponse(STATIC_DIR / "chatbox.html")


@router.get("/chat")
async def chat() -> FileResponse:
    return FileResponse(STATIC_DIR / "chat.html")


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
            "realtime_default_skills": REALTIME_DEFAULT_SKILLS,
            "default_prompt": DEFAULT_FINAL_PROMPT,
            "default_chat_prompt": DEFAULT_CHAT_PROMPT,
        }
    )


@router.get("/api/realtime/skills")
@router.get("/api/chatbox/skills")
async def chatbox_skills() -> JSONResponse:
    return JSONResponse({"skills": list_runtime_skills(), "default_skills": REALTIME_DEFAULT_SKILLS})
