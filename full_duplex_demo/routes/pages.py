from fastapi import APIRouter
from fastapi.responses import FileResponse

from full_duplex_demo.config import STATIC_DIR

router = APIRouter()
NO_CACHE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@router.get("/full_duplex")
async def full_duplex_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)
