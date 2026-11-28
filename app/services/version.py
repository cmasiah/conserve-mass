"""Compare the running version with the newest one published on GitHub.

Two release channels, baked into the image at build time (see Dockerfile):
  stable  image :latest, built from `main`. An update is a higher APP_VERSION.
  beta    image :beta, built from `beta`. Every new commit on `beta` is an update,
          even without a version bump, so it's compared by git commit instead.
"""
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import httpx

from app.config import APP_VERSION, get_settings
from app.db import utcnow

log = logging.getLogger(__name__)

CACHE_FOR = timedelta(minutes=10)
CHANNEL = "beta" if os.environ.get("APP_CHANNEL", "").strip().lower() == "beta" else "stable"
GIT_SHA = os.environ.get("APP_GIT_SHA", "").strip().lower()
if GIT_SHA in ("", "unknown"):
    GIT_SHA = ""
BRANCH = {"stable": "main", "beta": "beta"}[CHANNEL]
_VERSION_RE = re.compile(r'^APP_VERSION = "([^"]+)"', re.MULTILINE)


@dataclass
class VersionInfo:
    current: str = APP_VERSION
    latest: str | None = None
    commits: list[dict] = field(default_factory=list)
    checked_at: datetime | None = None
    error: str | None = None
    image_ready: bool = False  # a built image for `latest` exists on GHCR
    channel: str = CHANNEL
    current_sha: str = GIT_SHA
    latest_sha: str | None = None   # beta: head commit of the beta branch
    builds: list[dict] = field(default_factory=list)   # recent images you can switch to

    @property
    def newer(self) -> bool:
        if self.channel == "beta":
            # A local build doesn't know its commit; never nag it.
            return bool(self.latest_sha and self.current_sha
                        and self.latest_sha != self.current_sha)
        return bool(self.latest) and parse_version(self.latest) > parse_version(self.current)

    @staticmethod
    def _label(version: str | None, sha: str | None, channel: str) -> str | None:
        if version is None:
            return None
        return f"{version}-beta+{sha[:7]}" if channel == "beta" and sha else version

    @property
    def current_label(self) -> str:
        return self._label(self.current, self.current_sha, self.channel)

    @property
    def latest_label(self) -> str | None:
        return self._label(self.latest, self.latest_sha, self.channel)

    @property
    def update_available(self) -> bool:
        """Only offer an update once GitHub has finished building and publishing it."""
        return self.newer and self.image_ready

    @property
    def building(self) -> bool:
        return self.newer and not self.image_ready


_cache = VersionInfo()


def parse_version(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.split("."):
        m = re.match(r"\d+", p)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


def cached() -> VersionInfo:
    return _cache


MANIFEST_TYPES = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


async def image_published(client: httpx.AsyncClient, repo: str, tag: str) -> bool:
    """True if ghcr.io/<repo>:<tag> exists (anonymous pull token; image is public)."""
    path = repo.lower()
    t = await client.get("https://ghcr.io/token", params={"scope": f"repository:{path}:pull"})
    if t.status_code != 200:
        return False
    r = await client.head(f"https://ghcr.io/v2/{path}/manifests/{tag}",
                          headers={"Authorization": f"Bearer {t.json().get('token', '')}",
                                   "Accept": MANIFEST_TYPES})
    return r.status_code == 200


MAX_BUILDS = 12          # builds looked at; the page shows at most 1 newer + 3 older
MAX_OLDER = 3
_TAG_RE = re.compile(r"^(sha-[0-9a-f]{40}|\d+\.\d+\.\d+)$")


def valid_rollback_tag(tag: str) -> bool:
    """Only exact image tags: sha-<40 hex> (a beta build) or 1.2.3 (a release)."""
    return bool(_TAG_RE.match(tag or ""))


async def _ghcr_tags(client: httpx.AsyncClient, repo: str) -> list[str]:
    path = repo.lower()
    t = await client.get("https://ghcr.io/token", params={"scope": f"repository:{path}:pull"})
    if t.status_code != 200:
        return []
    r = await client.get(f"https://ghcr.io/v2/{path}/tags/list", params={"n": 1000},
                         headers={"Authorization": f"Bearer {t.json().get('token', '')}"})
    return r.json().get("tags", []) if r.status_code == 200 else []


def trim_builds(builds: list[dict]) -> list[dict]:
    """Newest first in, at most: the newest build above the running one (an upgrade),
    the running one, and up to MAX_OLDER below it (rollbacks). Each gets a "kind"."""
    idx = next((i for i, b in enumerate(builds) if b["current"]), None)
    if idx is None:   # running build not in the list (too old, or a local build)
        return [{**b, "kind": "upgrade" if i == 0 else "rollback"}
                for i, b in enumerate(builds[:1 + MAX_OLDER])]
    out = []
    if idx > 0:
        out.append({**builds[0], "kind": "upgrade"})
    out.append({**builds[idx], "kind": "current"})
    out += [{**b, "kind": "rollback"} for b in builds[idx + 1: idx + 1 + MAX_OLDER]]
    return out


async def recent_builds(client: httpx.AsyncClient, repo: str, commits: list[dict]) -> list[dict]:
    """Images you can switch to (see trim_builds).
    beta: recent beta commits whose sha-<commit> image exists.
    stable: released version tags."""
    tags = set(await _ghcr_tags(client, repo))
    if CHANNEL == "beta":
        out = []
        for c in commits:
            tag = f"sha-{c.get('full_sha', '')}"
            if tag in tags:
                out.append({"tag": tag, "label": f"{c['sha']}", "message": c["message"],
                            "date": c["date"], "url": c["url"],
                            "current": c.get("full_sha") == GIT_SHA})
        return trim_builds(out[:MAX_BUILDS])
    versions = sorted((t for t in tags if re.match(r"^\d+\.\d+\.\d+$", t)),
                      key=parse_version, reverse=True)[:MAX_BUILDS]
    return trim_builds([{"tag": v, "label": v, "message": "", "date": None, "url": None,
                         "current": v == APP_VERSION} for v in versions])


async def check(force: bool = False) -> VersionInfo:
    global _cache
    if (not force and _cache.checked_at
            and utcnow() - _cache.checked_at < CACHE_FOR):
        return _cache

    repo = get_settings().update_repo
    info = VersionInfo(latest=_cache.latest, commits=_cache.commits,
                       image_ready=_cache.image_ready, latest_sha=_cache.latest_sha,
                       builds=_cache.builds)
    headers = {"User-Agent": get_settings().user_agent}
    try:
        async with httpx.AsyncClient(timeout=10, headers=headers) as client:
            r = await client.get(
                f"https://raw.githubusercontent.com/{repo}/{BRANCH}/app/config.py")
            r.raise_for_status()
            m = _VERSION_RE.search(r.text)
            if m:
                info.latest = m.group(1)

            c = await client.get(f"https://api.github.com/repos/{repo}/commits",
                                 params={"sha": BRANCH, "per_page": 10},
                                 headers={"Accept": "application/vnd.github+json"})
            if c.status_code == 200:
                data = c.json()
                if data:
                    info.latest_sha = data[0]["sha"].lower()
                info.commits = [{
                    "sha": x["sha"][:7],
                    "full_sha": x["sha"].lower(),
                    "message": x["commit"]["message"].splitlines()[0][:140],
                    "date": datetime.fromisoformat(
                        x["commit"]["committer"]["date"].replace("Z", "+00:00")),
                    "url": x["html_url"],
                } for x in data]

            if info.newer:
                tag = f"sha-{info.latest_sha}" if CHANNEL == "beta" else info.latest
                info.image_ready = await image_published(client, repo, tag)
            else:
                info.image_ready = False
            info.builds = await recent_builds(client, repo, info.commits)
    except (httpx.HTTPError, KeyError, ValueError) as e:
        info.error = f"Couldn't reach GitHub: {e}"
        log.warning("Version check failed: %s", e)
    info.checked_at = utcnow()
    _cache = info
    return info


RELEASE_SEEN_KEY = "release_notified"   # settings row: highest main version already pinged


async def main_release() -> tuple[str | None, bool]:
    """(APP_VERSION on main, whether its :<version> image is published)."""
    repo = get_settings().update_repo
    headers = {"User-Agent": get_settings().user_agent}
    async with httpx.AsyncClient(timeout=10, headers=headers) as client:
        r = await client.get(f"https://raw.githubusercontent.com/{repo}/main/app/config.py")
        r.raise_for_status()
        m = _VERSION_RE.search(r.text)
        if not m:
            return None, False
        return m.group(1), await image_published(client, repo, m.group(1))


def release_to_announce(main_version: str | None, image_ready: bool,
                        seen: str | None) -> str | None:
    """The main version to DM about, if any. First run only records a baseline."""
    if not main_version or not image_ready or seen is None:
        return None
    return main_version if parse_version(main_version) > parse_version(seen) else None


async def version_job() -> None:
    """Scheduled: refresh the System page's update info, and DM once per new MAIN release.

    Beta commits never ping, even on the beta channel: you're the one merging them.
    A new APP_VERSION reaching main (a promotion) pings once, on either channel."""
    await check(force=True)
    try:
        version, ready = await main_release()
    except httpx.HTTPError as e:
        log.info("Main release check failed: %s", e)
        return
    if not version:
        return
    from app.db import session_scope
    from app.models import Setting
    from app.services.notifier import enqueue
    with session_scope() as db:
        row = db.get(Setting, RELEASE_SEEN_KEY)
        if row is None:
            db.add(Setting(key=RELEASE_SEEN_KEY, value=version))  # baseline, no ping
            return
        new = release_to_announce(version, ready, row.value)
        if new is None:
            return
        row.value = new
        if CHANNEL == "stable":
            body = (f"You're running {APP_VERSION}. Update from the System page.")
        else:
            body = (f"Promoted to main (the stable :latest image). You're on beta "
                    f"({_cache.current_label}), which already has everything in it.")
        enqueue(db, ref=f"release:{new}", kind="update_available",
                title=f"🚀 Conserve Mass Ops {new} released", body=body,
                url=f"https://github.com/{get_settings().update_repo}/tree/main")
