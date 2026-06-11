import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from realtime_audio_demo.config import SILERO_VAD_ENABLED, SILERO_VAD_PRELOAD, STATIC_DIR
from realtime_audio_demo.routes import audio, chat, pages
from realtime_audio_demo.services.silero_vad import SileroVadUnavailable, preload_silero_vad


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if SILERO_VAD_ENABLED and SILERO_VAD_PRELOAD:
        try:
            status = await asyncio.to_thread(preload_silero_vad)
            app.state.silero_vad = status
            logger.info("Silero VAD preloaded on %s", status.get("device"))
        except SileroVadUnavailable as exc:
            app.state.silero_vad = {"preloaded": False, "error": str(exc)}
            logger.warning("Silero VAD preload skipped: %s", exc)
        except Exception as exc:
            app.state.silero_vad = {"preloaded": False, "error": str(exc)}
            logger.exception("Silero VAD preload failed")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Qwen3-Omni realtime audio demo", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(pages.router)
    app.include_router(chat.router)
    app.include_router(audio.router)
    return app


app = create_app()
