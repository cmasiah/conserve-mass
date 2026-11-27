import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.sessions import SessionMiddleware

from app.config import APP_NAME, APP_VERSION, get_settings
from app.db import engine
from app.discord_bot import bot
from app.esi.client import close_client
from app.jobs import scheduler
from app.logbuffer import setup_logging
from app.security import LoginRequired
from app.web.routes import auth, logs, pages

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


@app.get("/healthz", include_in_schema=False)
def healthz():
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return JSONResponse({"status": "ok", "version": APP_VERSION})


app.include_router(auth.router)
app.include_router(pages.router)
app.include_router(logs.router)
