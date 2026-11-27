"""Admin log viewer: live tail of the in-memory buffer + download of the log file."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.config import get_settings
from app.logbuffer import LEVELS, LOG_FILE_NAME, ring
from app.security import require_user
from app.web.templating import render

router = APIRouter(dependencies=[Depends(require_user)])


@router.get("/logs")
def logs_page(request: Request, level: str = "INFO", q: str = ""):
    log_dir = get_settings().config_dir / "logs"
    files = sorted(log_dir.glob(f"{LOG_FILE_NAME}*")) if log_dir.exists() else []
    return render(request, "logs.html", level=level.upper(), q=q, levels=list(LEVELS),
                  files=[(f.name, f.stat().st_size) for f in files])


@router.get("/logs/api")
def logs_api(after: int = 0, level: str = "INFO", q: str = "", limit: int = 1000):
    rows = ring.query(after=after, min_level=level, q=q, limit=min(limit, 5000))
    return JSONResponse({"rows": rows, "counts": ring.counts()})


@router.get("/logs/download/{name}")
def logs_download(name: str):
    if not name.startswith(LOG_FILE_NAME) or "/" in name or ".." in name:
        raise HTTPException(404)
    path = get_settings().config_dir / "logs" / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="text/plain", filename=name)
