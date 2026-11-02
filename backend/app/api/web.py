"""빌드된 프런트엔드를 내준다. 소스는 frontend/, 빌드는 pnpm build."""

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings


def mount_web(app: FastAPI, settings: Settings) -> None:
    """라우트를 다 등록한 뒤 부른다. 빌드가 없으면 API만 뜬다."""
    assets = settings.web_dist_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")


router = APIRouter()


@router.get("/{path:path}", include_in_schema=False)
async def spa(path: str, settings: Settings = Depends(get_settings)) -> FileResponse:  # noqa: B008
    dist = settings.web_dist_dir
    if path.startswith("api/"):
        raise HTTPException(404, "not found")
    candidate = (dist / path).resolve()
    if path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
        return FileResponse(candidate)
    index = dist / "index.html"
    if not index.is_file():
        raise HTTPException(503, "frontend is not built; run pnpm build in frontend/")
    return FileResponse(index)
