import secrets
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.config import APP_NAME, APP_VERSION
from app.models.couriers import WORK_STATUSES
from app.services import version
from app.utils import fmt_delta, fmt_dt, fmt_isk, fmt_m3

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters.update(isk=fmt_isk, m3=fmt_m3, dt=fmt_dt, delta=fmt_delta)
templates.env.globals.update(APP_NAME=APP_NAME, APP_VERSION=APP_VERSION,
                             WORK_STATUSES=WORK_STATUSES,
                             version_info=version.cached,  # cached only, never blocks
                             # Changes with every build, so browsers fetch fresh CSS/icons
                             # after an update instead of reusing a stale cached copy.
                             ASSET_V=(version.GIT_SHA[:10] or APP_VERSION))


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def render(request: Request, name: str, **ctx):
    ctx.setdefault("user", request.session.get("user"))
    ctx["csrf"] = csrf_token(request)
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, message: str, level: str = "ok") -> None:
    request.session["flash"] = {"msg": message, "level": level}
