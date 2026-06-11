from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from realtime_audio_demo.config import STATIC_DIR
from realtime_audio_demo.routes import audio, chat, pages


def create_app() -> FastAPI:
    app = FastAPI(title="Qwen3-Omni realtime audio demo")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(pages.router)
    app.include_router(chat.router)
    app.include_router(audio.router)
    return app


app = create_app()
