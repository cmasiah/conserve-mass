"""Compare the running version with the newest one published on GitHub."""
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from app.config import APP_VERSION, get_settings
from app.db import utcnow

log = logging.getLogger(__name__)

CACHE_FOR = timedelta(minutes=10)
_VERSION_RE = re.compile(r'^APP_VERSION = "([^"]+)"', re.MULTILINE)


@dataclass
class VersionInfo:
    current: str = APP_VERSION
    latest: str | None = None
    commits: list[dict] = field(default_factory=list)
    checked_at: datetime | None = None
    error: str | None = None

    @property
    def update_available(self) -> bool:
        return bool(self.latest) and parse_version(self.latest) > parse_version(self.current)


_cache = VersionInfo()


def parse_version(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def cached() -> VersionInfo:
    return _cache


async def check(force: bool = False) -> VersionInfo:
    global _cache
    if (not force and _cache.checked_at
            and utcnow() - _cache.checked_at < CACHE_FOR):
        return _cache

    repo = get_settings().update_repo
    info = VersionInfo(latest=_cache.latest, commits=_cache.commits)
    headers = {"User-Agent": get_settings().user_agent}
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            r = await client.get(f"https://raw.githubusercontent.com/{repo}/main/app/config.py")
            r.raise_for_status()
            m = _VERSION_RE.search(r.text)
            if m:
                info.latest = m.group(1)

            c = await client.get(f"https://api.github.com/repos/{repo}/commits",
                                 params={"sha": "main", "per_page": 10},
                                 headers={"Accept": "application/vnd.github+json"})
            if c.status_code == 200:
                info.commits = [{
                    "sha": x["sha"][:7],
                    "message": x["commit"]["message"].splitlines()[0][:140],
                    "date": datetime.fromisoformat(
                        x["commit"]["committer"]["date"].replace("Z", "+00:00")),
                    "url": x["html_url"],
                } for x in c.json()]
    except (httpx.HTTPError, KeyError, ValueError) as e:
        info.error = f"Couldn't reach GitHub: {e}"
        log.warning("Version check failed: %s", e)
    info.checked_at = utcnow()
    _cache = info
    return info


async def version_job() -> None:
    """Scheduled: refresh the cache and DM once per new version."""
    info = await check(force=True)
    if not info.update_available:
        return
    from app.db import session_scope
    from app.services.notifier import enqueue
    changes = "\n".join(f"• {c['message']}" for c in info.commits[:5])
    with session_scope() as db:
        enqueue(db, ref=f"version:{info.latest}", kind="update_available",
                title=f"⬆️ Conserve Mass Ops {info.latest} is available",
                body=f"You're running {info.current}.\n\n{changes}\n\n"
                     f"Update from the System page.",
                url=f"{get_settings().base_url}/system")
