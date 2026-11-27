"""EVE SSO login, character linking, logout."""
import logging
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.esi import client as esi
from app.esi import sso
from app.models import Character, Corporation, EsiToken
from app.security import encrypt, require_csrf
from app.web.templating import flash, render

log = logging.getLogger(__name__)
router = APIRouter()

CONTRACT_SCOPE = "esi-contracts.read_corporation_contracts.v1"


@router.get("/login")
def login_page(request: Request):
    missing = get_settings().missing_required()
    return render(request, "login.html", missing=missing)


@router.get("/auth/login")
def start_login(request: Request, profile: str = "courier"):
    if get_settings().missing_required():
        return RedirectResponse("/login", status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_profile"] = profile
    scopes = sso.SCOPE_PROFILES.get(profile, sso.COURIER_SCOPES)
    return RedirectResponse(sso.authorize_url(state, scopes), status_code=303)


async def _ensure_tracked_corp(db: Session, char: Character, scopes: list[str]) -> None:
    s = get_settings()
    target = s.corporation_id or char.corporation_id
    if not target or char.corporation_id != target:
        return
    corp = db.get(Corporation, target)
    if corp is None:
        corp = Corporation(corporation_id=target)
        db.add(corp)
        try:
            info = await esi.get(f"/corporations/{target}")
            corp.name, corp.ticker = info.get("name"), info.get("ticker")
        except esi.EsiError as e:
            log.warning("Could not load corporation %s: %s", target, e)
    corp.is_tracked = True
    if CONTRACT_SCOPE in scopes and (corp.sync_character_id is None or char.is_admin):
        corp.sync_character_id = char.character_id


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = "",
                   db: Session = Depends(get_db)):
    expected = request.session.pop("oauth_state", None)
    if not code or not state or state != expected:
        flash(request, "Login failed: invalid or expired state. Try again.", "error")
        return RedirectResponse("/login", status_code=303)

    try:
        tok = await sso.exchange_code(code)
    except sso.SSOError as e:
        log.warning("SSO exchange failed: %s", e)
        flash(request, "EVE SSO rejected the login. Check ESI_CLIENT_ID/SECRET and callback URL.",
              "error")
        return RedirectResponse("/login", status_code=303)

    s = get_settings()
    logged_in = request.session.get("user")
    char = db.get(Character, tok.character_id)
    any_admin = db.scalar(select(Character).where(Character.is_admin.is_(True))) is not None

    # Who may log in: ADMIN_CHARACTER_IDS, an existing admin, or the very first login.
    allowed = (tok.character_id in s.admin_ids if s.admin_ids
               else (char is not None and char.is_admin) or not any_admin)
    if not logged_in and not allowed:
        log.warning("Rejected login from %s (%s)", tok.character_name, tok.character_id)
        flash(request, f"{tok.character_name} is not authorized for this app.", "error")
        return RedirectResponse("/login", status_code=303)

    try:
        pub = await esi.get(f"/characters/{tok.character_id}")
    except esi.EsiError:
        pub = {}

    if char is None:
        char = Character(character_id=tok.character_id, name=tok.character_name)
        db.add(char)
    char.name = tok.character_name
    char.corporation_id = pub.get("corporation_id", char.corporation_id)
    char.alliance_id = pub.get("alliance_id")
    if not logged_in and allowed:
        char.is_admin = True

    token = db.get(EsiToken, char.character_id) or EsiToken(character_id=char.character_id)
    token.refresh_token_enc = encrypt(tok.refresh_token)
    token.access_token_enc = encrypt(tok.access_token)
    token.access_expires_at = tok.expires_at
    token.scopes = " ".join(tok.scopes)
    token.is_valid = True
    token.last_error = None
    token.updated_at = utcnow()
    db.add(token)
    db.flush()

    await _ensure_tracked_corp(db, char, tok.scopes)
    db.commit()

    if logged_in:
        onboard = request.session.get("onboard")
        if onboard is not None:
            # Bulk linking: remember who was just linked and go straight back to the
            # onboarding page, which offers (or auto-starts) the next EVE login.
            onboard["linked"] = [n for n in onboard.get("linked", []) if n != char.name] + [char.name]
            request.session["onboard"] = onboard
            return RedirectResponse("/characters/onboard", status_code=303)
        flash(request, f"Linked {char.name}.")
        return RedirectResponse("/characters", status_code=303)

    request.session["user"] = {"character_id": char.character_id, "name": char.name}
    flash(request, f"Welcome, {char.name}. Fly safe o7")
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    require_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
