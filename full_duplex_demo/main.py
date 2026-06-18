import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from realtime_audio_demo.config import (
    EASYTURN_CHECKPOINT,
    EASYTURN_CONFIG,
    EASYTURN_ENABLED,
    EASYTURN_LLM_PATH,
    EASYTURN_PRELOAD,
    SILERO_VAD_ENABLED,
    SILERO_VAD_PRELOAD,
    STATIC_DIR,
)
from realtime_audio_demo.session_store import cleanup_expired_sessions

logger = logging.getLogger(__name__)


async def _session_cleanup_loop() -> None:
    while True:
        try:
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            return
        try:
            await cleanup_expired_sessions()
        except Exception:
            logger.exception("session cleanup failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "EasyTurn config enabled=%s preload=%s config=%s checkpoint_configured=%s llm_path_configured=%s",
        EASYTURN_ENABLED,
        EASYTURN_PRELOAD,
        EASYTURN_CONFIG,
        bool(EASYTURN_CHECKPOINT),
        bool(EASYTURN_LLM_PATH),
    )

    if SILERO_VAD_ENABLED and SILERO_VAD_PRELOAD:
        try:
            from realtime_audio_demo.services.silero_vad import preload_silero_vad, SileroVadUnavailable

            status = await asyncio.to_thread(preload_silero_vad)
            app.state.silero_vad = status
            logger.info("Silero VAD preloaded on %s", status.get("device"))
        except SileroVadUnavailable as exc:
            app.state.silero_vad = {"preloaded": False, "error": str(exc)}
            logger.warning("Silero VAD preload skipped: %s", exc)
        except Exception as exc:
            app.state.silero_vad = {"preloaded": False, "error": str(exc)}
            logger.exception("Silero VAD preload failed")

    if EASYTURN_ENABLED and EASYTURN_PRELOAD:
        try:
            from easy_turn.service import preload_easy_turn

            await asyncio.to_thread(preload_easy_turn)
            app.state.easy_turn = {"preloaded": True}
            logger.info("EasyTurn preloaded")
        except Exception as exc:
            app.state.easy_turn = {"preloaded": False, "error": str(exc)}
            logger.exception("EasyTurn preload failed")

    cleanup_task = asyncio.create_task(_session_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


def create_app() -> FastAPI:
    app = FastAPI(title="Qwen3-Omni Realtime Audio Demo", lifespan=lifespan)

    # Static files from old project (recorder-worklet.js, etc.)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Old routes (chatbox, demo, /ws/audio, /ws/vad, etc.)
    from realtime_audio_demo.routes import pages as old_pages
    from realtime_audio_demo.routes import chat as old_chat
    from realtime_audio_demo.routes import audio as old_audio

    app.include_router(old_pages.router)
    app.include_router(old_chat.router)
    app.include_router(old_audio.router)

    # New full-duplex routes (/full_duplex page, /ws/full_duplex)
    from full_duplex_demo.routes.pages import router as fd_pages_router
    from full_duplex_demo.routes.ws import router as fd_ws_router

    app.include_router(fd_pages_router)
    app.include_router(fd_ws_router)

    return app


app = create_app()
