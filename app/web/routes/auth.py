"""EVE SSO login, character linking, logout."""
import logging
import secrets
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace

import httpx

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope, utcnow, with_retry
from app.esi import client as esi
from app.esi import sso
from app.models import Character, Corporation, EntityName, EsiToken
from app.security import encrypt, require_csrf
from app.services import corps
from app.web.templating import flash, render

log = logging.getLogger(__name__)
router = APIRouter()

CONTRACT_SCOPE = "esi-contracts.read_corporation_contracts.v1"


@router.get("/login")
def login_page(request: Request):
    missing = get_settings().missing_required()
    return render(request, "login.html", missing=missing)


def _clones_ok() -> bool:
    """True once a login has granted the implants/clones scopes, which proves the EVE app
    has them ticked; from then on every Member / Director+ link asks for them."""
    from app.models import Setting
    with session_scope() as db:
        return db.get(Setting, sso.CLONES_OK_KEY) is not None


@router.get("/auth/login")
def start_login(request: Request, profile: str = "courier", clones: int = 0):
    if get_settings().missing_required():
        return RedirectResponse("/login", status_code=303)
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_profile"] = profile
    scopes = sso.scopes_for(profile, bool(clones) or _clones_ok())
    return RedirectResponse(sso.authorize_url(state, scopes), status_code=303)


# The callback waits on CCP three times (token exchange, character, corporation). All of
# that happens first; the database is then read and written in short, self-contained
# steps on a worker thread, so a busy database can never freeze the app mid-login and a
# busy moment is retried instead of losing the (single-use) login code.

@dataclass
class _Known:
    char_exists: bool
    char_is_admin: bool
    char_corp_id: int | None
    any_admin: bool


def _lookup_known(character_id: int) -> _Known:
    with session_scope() as db:
        char = db.get(Character, character_id)
        return _Known(
            char_exists=char is not None,
            char_is_admin=bool(char and char.is_admin),
            char_corp_id=char.corporation_id if char else None,
            any_admin=db.scalar(select(Character.character_id).where(
                Character.is_admin.is_(True)).limit(1)) is not None)


def _corp_known(corp_id: int) -> bool:
    with session_scope() as db:
        return db.get(Corporation, corp_id) is not None


# Which link the character came through (the EVE consent page's scope set) decides its
# role: Member = only its own data, Director+ = may also read its corp's data.
PROFILE_ROLE = {"personal": "member", "industry": "director"}


def _link_corp(db: Session, char: Character, scopes: set[str], corp_info: dict | None) -> None:
    """List the character's corp (Corporations switch). Only the home corp is tracked by
    default, and only its characters can poll contracts."""
    if not char.corporation_id:
        return
    corp, _ = corps.ensure_corp(db, char.corporation_id, corp_info)
    if corp.name:
        ent = db.get(EntityName, corp.corporation_id) or EntityName(entity_id=corp.corporation_id)
        ent.name, ent.category, ent.resolved_at = corp.name, "corporation", utcnow()
        db.add(ent)
    if corp.is_home and CONTRACT_SCOPE in scopes and (
            corp.sync_character_id is None or char.is_admin):
        corp.sync_character_id = char.character_id


@dataclass
class Saved:
    name: str
    kept_existing: bool = False  # a narrower re-link didn't replace a broader token


def save_login(tok: sso.TokenResult, pub: dict, corp_info: dict | None,
               make_admin: bool, profile: str = "") -> Saved:
    """Store the character + its token in one short transaction.

    profile = the link used: "personal" (Member) or "industry" (Director+) set the role
    and replace the token as chosen, even if that means fewer scopes. Any other link
    (plain sign-in, contracts only) never shrinks an existing broader token."""
    with session_scope() as db:
        char = db.get(Character, tok.character_id)
        if char is None:
            char = Character(character_id=tok.character_id, name=tok.character_name)
            db.add(char)
        char.name = tok.character_name
        char.corporation_id = pub.get("corporation_id", char.corporation_id)
        char.alliance_id = pub.get("alliance_id")
        if make_admin:
            char.is_admin = True

        if profile in PROFILE_ROLE:
            char.link_role = PROFILE_ROLE[profile]

        token = db.get(EsiToken, char.character_id) or EsiToken(character_id=char.character_id)
        if (profile not in PROFILE_ROLE and token.is_valid and token.scopes
                and set(tok.scopes) < token.scope_set):
            # E.g. the director re-linked through "Personal only": keep the broader token
            # instead of silently losing corp access. (EVE refresh tokens are independent.)
            db.flush()
            _link_corp(db, char, token.scope_set, corp_info)
            return Saved(char.name, kept_existing=True)
        token.refresh_token_enc = encrypt(tok.refresh_token)
        token.access_token_enc = encrypt(tok.access_token)
        token.access_expires_at = tok.expires_at
        token.scopes = " ".join(tok.scopes)
        if set(sso.CLONE_SCOPES) <= set(tok.scopes):
            from app.models import Setting
            db.merge(Setting(key=sso.CLONES_OK_KEY, value="1"))
        token.is_valid = True
        token.last_error = None
        token.updated_at = utcnow()
        db.add(token)
        db.flush()
        _link_corp(db, char, set(tok.scopes), corp_info)
        return Saved(char.name)


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = ""):
    expected = request.session.pop("oauth_state", None)
    profile = request.session.pop("oauth_profile", "")
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
    known = await run_in_threadpool(with_retry, partial(_lookup_known, tok.character_id),
                                    what="login check")

    # Who may log in: ADMIN_CHARACTER_IDS, an existing admin, or the very first login.
    allowed = (tok.character_id in s.admin_ids if s.admin_ids
               else known.char_is_admin or not known.any_admin)
    if not logged_in and not allowed:
        log.warning("Rejected login from %s (%s)", tok.character_name, tok.character_id)
        flash(request, f"{tok.character_name} is not authorized for this app.", "error")
        return RedirectResponse("/login", status_code=303)

    try:
        pub = await esi.get(f"/characters/{tok.character_id}")
    except (esi.EsiError, httpx.HTTPError):
        pub = {}

    corp_info = None
    char_corp = pub.get("corporation_id", known.char_corp_id)
    if char_corp and not await run_in_threadpool(
            with_retry, partial(_corp_known, char_corp), what="login check"):
        try:  # name + ticker for the Corporations list
            corp_info = await esi.get(f"/corporations/{char_corp}")
        except (esi.EsiError, httpx.HTTPError) as e:
            log.warning("Could not load corporation %s: %s", char_corp, e)

    saved = await run_in_threadpool(
        with_retry, partial(save_login, tok, pub, corp_info, not logged_in and allowed,
                            profile),
        what=f"saving login for {tok.character_name}")
    char = SimpleNamespace(character_id=tok.character_id, name=saved.name)
    if saved.kept_existing:
        flash(request, f"{saved.name} already had broader access, so its existing token "
                       f"was kept.")

    if logged_in:
        onboard = request.session.get("onboard")
        if onboard is not None:
            # Bulk linking: remember who was just linked and go straight back to the
            # onboarding page, which offers (or auto-starts) the next EVE login.
            onboard["linked"] = [n for n in onboard.get("linked", []) if n != char.name] + [char.name]
            request.session["onboard"] = onboard
            return RedirectResponse("/characters/onboard", status_code=303)
        if not saved.kept_existing:
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
