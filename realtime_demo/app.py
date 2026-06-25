from full_duplex_demo.main import app
from realtime_demo.routes.pages import router as realtime_pages_router
from realtime_demo.routes.ws import router as realtime_ws_router

# full_duplex_demo imports the legacy realtime_audio_demo routes, where
# /realtime redirects to /chatbox. This entrypoint owns /realtime now.
app.router.routes = [
    route for route in app.router.routes
    if getattr(route, "path", None) != "/realtime"
]
app.include_router(realtime_pages_router)
app.include_router(realtime_ws_router)

__all__ = ["app"]
