"""EVE SSO (OAuth2 v2): authorize URL, code exchange, refresh, JWT validation."""
import asyncio
import base64
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from app.config import get_settings
from app.db import release_write_lock

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
JWKS_URL = "https://login.eveonline.com/oauth/jwks"
ISSUERS = ("https://login.eveonline.com", "login.eveonline.com")

# Scope sets, by who's linking (EVE shows the list on its consent page):
#   courier   the contract poller: corp contracts + structure names.
#   personal  "Member": any character, any corp, no roles needed: only its OWN skills, jobs,
#             blueprints and assets. For alts and members outside Conserve Mass.
#   industry  "Director+": personal + corp jobs/blueprints/assets/divisions
#             + courier. Corp scopes only return data if the character has the roles.
COURIER_SCOPES = [
    "esi-contracts.read_corporation_contracts.v1",
    "esi-universe.read_structures.v1",
]
PERSONAL_SCOPES = [
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
    "esi-industry.read_character_jobs.v1",
    "esi-characters.read_blueprints.v1",
    "esi-assets.read_assets.v1",
    "esi-universe.read_structures.v1",      # names of structures its jobs/assets sit in
]
# Implants, jump clones (Characters › Implants). Optional: EVE rejects the whole login with
# "invalid_scope" unless they're also ticked on the app at developers.eveonline.com, so
# they're only asked for on an "with implants" link, or once one such link has worked.
CLONE_SCOPES = [
    "esi-clones.read_implants.v1",
    "esi-clones.read_clones.v1",
]
CORP_INDUSTRY_SCOPES = [
    "esi-industry.read_corporation_jobs.v1",
    "esi-assets.read_corporation_assets.v1",
    "esi-corporations.read_blueprints.v1",
    "esi-corporations.read_divisions.v1",   # corp hangar division names
]
INDUSTRY_SCOPES = [s for s in PERSONAL_SCOPES if s not in COURIER_SCOPES] + CORP_INDUSTRY_SCOPES
SCOPE_PROFILES = {
    "courier": COURIER_SCOPES,
    "personal": PERSONAL_SCOPES,
    "industry": COURIER_SCOPES + INDUSTRY_SCOPES,
    "login": [],
}
CLONE_PROFILES = ("personal", "industry")
CLONES_OK_KEY = "sso:clone_scopes_ok"   # set once a login has actually granted them


def scopes_for(profile: str, clones: bool = False) -> list[str]:
    scopes = list(SCOPE_PROFILES.get(profile, COURIER_SCOPES))
    if clones and profile in CLONE_PROFILES:
        scopes += [s for s in CLONE_SCOPES if s not in scopes]
    return scopes


PROFILE_LABELS = {"personal": "Member", "industry": "Director+", "courier": "Contracts only"}

_jwks_client: PyJWKClient | None = None


class SSOError(Exception):
    pass


class TokenRevoked(SSOError):
    """Refresh token no longer valid (revoked, or scopes changed). User must re-login."""


@dataclass
class TokenResult:
    access_token: str
    refresh_token: str
    expires_at: datetime
    character_id: int
    character_name: str
    scopes: list[str]


def authorize_url(state: str, scopes: list[str]) -> str:
    s = get_settings()
    params = {
        "response_type": "code",
        "redirect_uri": s.sso_callback_url,
        "client_id": s.esi_client_id,
        "scope": " ".join(scopes),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth() -> str:
    s = get_settings()
    raw = f"{s.esi_client_id}:{s.esi_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _validate_jwt(access_token: str) -> dict:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=3600)
    key = _jwks_client.get_signing_key_from_jwt(access_token)
    claims = jwt.decode(
        access_token,
        key.key,
        algorithms=["RS256"],
        audience=[get_settings().esi_client_id, "EVE Online"],
        options={"verify_iss": False},
        leeway=30,
    )
    if claims.get("iss") not in ISSUERS:
        raise SSOError(f"Unexpected token issuer {claims.get('iss')!r}")
    return claims


async def _token_request(data: dict) -> TokenResult:
    s = get_settings()
    release_write_lock("EVE SSO")  # never wait on the network holding the DB write lock
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            TOKEN_URL,
            data=data,
            headers={"Authorization": _basic_auth(), "User-Agent": s.user_agent,
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code in (400, 401) and data.get("grant_type") == "refresh_token":
        raise TokenRevoked(resp.text[:300])
    if resp.status_code != 200:
        raise SSOError(f"SSO token request failed: {resp.status_code} {resp.text[:300]}")

    payload = resp.json()
    claims = await asyncio.to_thread(_validate_jwt, payload["access_token"])
    scp = claims.get("scp", [])
    if isinstance(scp, str):
        scp = [scp]
    sub = claims["sub"]  # "CHARACTER:EVE:<id>"
    return TokenResult(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or data.get("refresh_token", ""),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=int(payload["expires_in"])),
        character_id=int(sub.split(":")[-1]),
        character_name=claims.get("name", ""),
        scopes=scp,
    )


async def exchange_code(code: str) -> TokenResult:
    return await _token_request({"grant_type": "authorization_code", "code": code})


async def refresh(refresh_token: str) -> TokenResult:
    return await _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
