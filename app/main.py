import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from starlette.middleware.sessions import SessionMiddleware

from app.config import APP_NAME, APP_VERSION, get_settings
from app.db import engine, is_locked_error, utcnow
from app.discord_bot import bot
from app.esi.client import close_client
from app.jobs import scheduler
from app.logbuffer import setup_logging
from app.security import LoginRequired
from app.web.routes import assets as assets_routes, auth, jump as jump_routes, industry, logs, pages, settings as settings_routes
from app.web.templating import templates

settings = get_settings()
setup_logging(settings)
log = logging.getLogger("conserve-mass")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    missing = settings.missing_required()
    if missing:
        log.error("Missing required settings: %s — the UI will show setup help.", ", ".join(missing))
    log.info("%s %s starting (config dir %s)", APP_NAME, APP_VERSION, settings.config_dir)
    if not settings.esi_contact:
        log.warning("ESI_CONTACT is empty. CCP asks every app to send a contact email in its "
                    "User-Agent so they can reach you before throttling or banning it.")
    log.info("ESI User-Agent: %s", settings.user_agent)
    scheduler.start()
    bot.start_background()
    yield
    scheduler.shutdown()
    await bot.stop()
    await close_client()


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan,
              docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret or "dev-only-insecure",
    session_cookie="cmass_session",
    max_age=60 * 60 * 24 * 14,
    same_site="lax",
    https_only=settings.base_url.startswith("https://"),
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"),
          name="static")


@app.exception_handler(LoginRequired)
async def _login_required(_request: Request, _exc: LoginRequired):
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------ error pages
# Instead of a bare "Internal Server Error": say what happened and link back to where
# you were. error.html needs no session or database, so it renders even mid-outage.

def _back_url(request: Request) -> str:
    """Where 'Go back' should point: the page you came from, if it's this app."""
    ref = request.headers.get("referer") or ""
    host = request.headers.get("host") or ""
    if ref and host:
        from urllib.parse import urlsplit
        parts = urlsplit(ref)
        if parts.netloc == host and parts.path.startswith("/") and parts.path != request.url.path:
            return parts.path + (f"?{parts.query}" if parts.query else "")
    return "/"


def error_page(request: Request, status: int, *, icon: str, title: str, message: str,
               hint: str = ""):
    if "text/html" not in request.headers.get("accept", "text/html"):
        return JSONResponse({"error": title, "detail": message}, status_code=status)
    is_get = request.method in ("GET", "HEAD")
    if not is_get:
        hint = (hint + " " if hint else "") + "Nothing was saved, so it's safe to submit again."
    back = _back_url(request)
    return templates.TemplateResponse(request, "error.html", {
        "status": status, "icon": icon, "title": title, "message": message, "hint": hint,
        "retry_url": str(request.url.path) + (f"?{request.url.query}" if request.url.query
                                               else "") if is_get else None,
        "back_url": back, "back_label": "Go back" if back != "/" else "Home",
        "when": f"{utcnow():%Y-%m-%d %H:%M:%S}",
    }, status_code=status)


@app.exception_handler(OperationalError)
async def _db_error(request: Request, exc: OperationalError):
    if is_locked_error(exc):
        log.warning("Database busy on %s %s: %s", request.method, request.url.path, exc.orig)
        return error_page(
            request, 503, icon="⏳", title="The database was busy",
            message="A background sync was saving at the same moment. "
                    "This usually clears in a second or two.",
            hint="If it keeps happening, check the logs for the sync that's holding it up.")
    log.error("Database error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_page(request, 500, icon="⚠️", title="Database error",
                      message="The database refused that request. The details are in the logs.")


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception):
    # Starlette still logs the full traceback after this returns.
    return error_page(request, 500, icon="⚠️", title="Something went wrong",
                      message=f"The page hit an unexpected error ({type(exc).__name__}). "
                              "The details are in the logs.")


@app.get("/healthz", include_in_schema=False)
def healthz():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return JSONResponse({"status": "ok", "version": APP_VERSION})


app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(logs.router)
app.include_router(industry.router)
app.include_router(assets_routes.router)
app.include_router(jump_routes.router)
app.include_router(settings_routes.router)
