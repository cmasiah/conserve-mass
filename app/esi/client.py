"""Small ESI client: compatibility date, pagination, ETag/Expires, error-limit backoff,
and per-character access-token management."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.esi import sso
from app.models import EsiToken, SyncState
from app.security import decrypt, encrypt

log = logging.getLogger(__name__)

ESI_BASE = "https://esi.evetech.net"
# ESI date-based versioning. Bump after reviewing the changelog.
COMPATIBILITY_DATE = "2025-08-26"


class EsiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"ESI {status}: {message}")
        self.status = status


class NoValidToken(Exception):
    pass


@dataclass
class EsiResult:
    data: Any
    not_modified: bool
    expires_at: datetime | None


_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=ESI_BASE,
            timeout=30,
            headers={
                "User-Agent": get_settings().user_agent,
                "X-Compatibility-Date": COMPATIBILITY_DATE,
                "Accept": "application/json",
            },
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _parse_expires(resp: httpx.Response) -> datetime | None:
    raw = resp.headers.get("Expires")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


LOW_BUCKET_SLEEP = 30  # seconds to pause when a rate-limit bucket is nearly empty


def rate_limit_low(remaining: str | None, limit: str | None) -> bool:
    """True when X-Ratelimit-Remaining is under 10% of the bucket (min 10 tokens).
    `limit` looks like "150/15m"."""
    if remaining is None or limit is None:
        return False
    try:
        total = int(limit.split("/")[0])
        return int(remaining) < max(10, total // 10)
    except ValueError:
        return False


async def _respect_rate_limit(resp: httpx.Response) -> None:
    rem = resp.headers.get("X-Ratelimit-Remaining")
    lim = resp.headers.get("X-Ratelimit-Limit")
    if rate_limit_low(rem, lim):
        group = resp.headers.get("X-Ratelimit-Group", "?")
        log.warning("ESI rate-limit bucket '%s' low (%s of %s left); pausing %ss",
                    group, rem, lim, LOW_BUCKET_SLEEP)
        await asyncio.sleep(LOW_BUCKET_SLEEP)


async def _send(method: str, path: str, *, token: str | None = None,
                params: dict | None = None, json: Any = None,
                headers: dict | None = None) -> httpx.Response:
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    for attempt in range(4):
        resp = await client().request(method, path, params=params, json=json, headers=hdrs)

        # Legacy error-limit: back off before CCP bans the IP.
        remain = resp.headers.get("X-ESI-Error-Limit-Remain")
        if remain is not None and int(remain) < 20:
            reset = int(resp.headers.get("X-ESI-Error-Limit-Reset", "60"))
            log.warning("ESI error limit low (%s), sleeping %ss", remain, reset)
            await asyncio.sleep(reset + 1)

        # New token-bucket limit: ease off before the bucket is empty, not after a 429.
        await _respect_rate_limit(resp)

        if resp.status_code in (420, 429):
            wait = int(resp.headers.get("Retry-After") or resp.headers.get(
                "X-ESI-Error-Limit-Reset") or 60)
            log.warning("ESI rate limited (%s); retrying in %ss", resp.status_code, wait)
            await asyncio.sleep(wait + 1)
            continue
        if resp.status_code in (502, 503, 504) and attempt < 3:
            await asyncio.sleep(2 ** attempt * 5)
            continue
        return resp
    return resp


async def get(path: str, *, token: str | None = None, params: dict | None = None) -> Any:
    resp = await _send("GET", path, token=token, params=params)
    if resp.status_code != 200:
        raise EsiError(resp.status_code, resp.text[:300])
    return resp.json()


async def post(path: str, json: Any, *, token: str | None = None) -> Any:
    resp = await _send("POST", path, token=token, json=json)
    if resp.status_code != 200:
        raise EsiError(resp.status_code, resp.text[:300])
    return resp.json()


async def get_cached_paged(db: Session, key: str, path: str, *, token: str | None = None
                           ) -> EsiResult:
    """GET a (possibly paginated) list, honouring Expires and ETag via sync_state.

    Returns not_modified=True if ESI says nothing changed (or cache hasn't expired).
    """
    state = db.get(SyncState, key) or SyncState(key=key)
    db.add(state)
    now = utcnow()
    state.last_attempt_at = now

    if state.expires_at and state.expires_at > now:
        return EsiResult(data=None, not_modified=True, expires_at=state.expires_at)

    headers = {"If-None-Match": state.etag} if state.etag else None
    resp = await _send("GET", path, token=token, params={"page": 1}, headers=headers)

    if resp.status_code == 304:
        state.expires_at = _parse_expires(resp)
        state.last_success_at = now
        state.last_error = None
        return EsiResult(data=None, not_modified=True, expires_at=state.expires_at)
    if resp.status_code != 200:
        state.last_error = f"{resp.status_code}: {resp.text[:300]}"
        raise EsiError(resp.status_code, resp.text[:300])

    data = list(resp.json())
    pages = int(resp.headers.get("X-Pages", "1"))
    for page in range(2, pages + 1):
        r = await _send("GET", path, token=token, params={"page": page})
        if r.status_code != 200:
            state.last_error = f"page {page}: {r.status_code}"
            raise EsiError(r.status_code, r.text[:300])
        data.extend(r.json())

    # Only trust the ETag for single-page results (each page has its own ETag).
    state.etag = resp.headers.get("ETag") if pages == 1 else None
    state.expires_at = _parse_expires(resp)
    state.last_success_at = now
    state.last_error = None
    return EsiResult(data=data, not_modified=False, expires_at=state.expires_at)


# ---------------------------------------------------------------- tokens

_refresh_locks: dict[int, asyncio.Lock] = {}


async def access_token(db: Session, character_id: int) -> str:
    """Return a valid access token for the character, refreshing if needed."""
    tok = db.get(EsiToken, character_id)
    if tok is None or not tok.is_valid:
        raise NoValidToken(f"No valid ESI token for character {character_id}")

    lock = _refresh_locks.setdefault(character_id, asyncio.Lock())
    async with lock:
        db.refresh(tok)
        if (tok.access_token_enc and tok.access_expires_at
                and tok.access_expires_at > utcnow() + timedelta(seconds=60)):
            return decrypt(tok.access_token_enc)
        try:
            result = await sso.refresh(decrypt(tok.refresh_token_enc))
        except sso.TokenRevoked as e:
            tok.is_valid = False
            tok.last_error = f"Refresh rejected: {e}"
            db.commit()
            from app.services.notifier import enqueue
            enqueue(db, ref=f"token:{character_id}", kind="token_invalid",
                    title="⚠️ ESI token revoked",
                    body=f"Character {character_id} must log in again to keep syncing.",
                    url=f"{get_settings().base_url}/characters")
            db.commit()
            raise NoValidToken(str(e)) from e

        tok.access_token_enc = encrypt(result.access_token)
        tok.refresh_token_enc = encrypt(result.refresh_token)
        tok.access_expires_at = result.expires_at
        tok.scopes = " ".join(result.scopes)
        tok.last_error = None
        db.commit()
        return result.access_token
