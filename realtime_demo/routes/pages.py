from fastapi import APIRouter
from fastapi.responses import FileResponse

from realtime_demo.config import STATIC_DIR

router = APIRouter()
NO_CACHE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@router.get("/realtime")
async def realtime_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "realtime.html", headers=NO_CACHE)
