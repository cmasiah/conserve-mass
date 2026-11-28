"""What 'Sync Now' does and when data refreshes on its own.

Two clocks decide when new data shows up:
  * our schedule: how often the app asks ESI (Settings, defaults below);
  * ESI's cache: how long CCP serves the same answer. Asking again sooner returns the
    same data, so neither the schedule nor Sync Now can beat it.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.models import SyncState
from app.utils import fmt_delta


@dataclass
class Feed:
    label: str
    job_id: str
    every: timedelta
    esi_cache: str                  # CCP's documented cache time, for the explanation
    prefixes: tuple[str, ...]
    next_run: datetime | None = None
    cached_until: datetime | None = None   # ESI serves the same data until this time
    last_success: datetime | None = None
    errors: list[str] = field(default_factory=list)
    esi_window: timedelta = timedelta(minutes=5)  # typical cache length, if no pull is known

    @property
    def next_start(self) -> datetime | None:
        """When the current wait for the next automatic check began (for the timer bar)."""
        return self.next_run - self.every if self.next_run else None

    @property
    def esi_start(self) -> datetime | None:
        """Start of ESI's current cache window: our last pull, if it's inside the window."""
        if not self.cached_until:
            return None
        if self.last_success and self.last_success < self.cached_until:
            return self.last_success
        return self.cached_until - self.esi_window

    @property
    def every_text(self) -> str:
        mins = int(self.every.total_seconds() // 60)
        if mins < 1:
            return f"every {int(self.every.total_seconds())}s"
        if mins % 60 == 0:
            h = mins // 60
            return "every hour" if h == 1 else f"every {h} hours"
        return f"every {mins} min"


def _feeds(kind: str) -> list[Feed]:
    s = get_settings()
    if kind == "contracts":
        return [Feed("Corp contracts", "contract_sync", timedelta(seconds=s.contract_poll_seconds),
                     "5 min", ("corp_contracts:",), esi_window=timedelta(minutes=5))]
    return [
        Feed("Skills & queues", "industry_skills",
             timedelta(minutes=s.industry_skills_poll_minutes), "2 min", ("skills:", "skillqueue:"),
             esi_window=timedelta(minutes=2)),
        Feed("Industry jobs", "industry_jobs",
             timedelta(minutes=s.industry_jobs_poll_minutes), "5 min", ("jobs:",), esi_window=timedelta(minutes=5)),
        Feed("Blueprints", "industry_blueprints",
             timedelta(minutes=s.industry_blueprints_poll_minutes), "1 hour", ("bps:", "divisions:"),
             esi_window=timedelta(hours=1)),
        Feed("Assets", "industry_assets", timedelta(minutes=s.assets_poll_minutes), "1 hour",
             ("assetsync:",), esi_window=timedelta(hours=1)),
    ]


def _next_run(job_id: str) -> datetime | None:
    from app.jobs.scheduler import scheduler
    if not scheduler.running:
        return None
    job = scheduler.get_job(job_id)
    if job is None or job.next_run_time is None:
        return None
    return job.next_run_time.astimezone(timezone.utc)


def feeds(db: Session, kind: str, now: datetime | None = None) -> list[Feed]:
    """kind: 'contracts' or 'industry'."""
    now = now or utcnow()
    out = _feeds(kind)
    for f in out:
        f.next_run = _next_run(f.job_id)
        rows = db.scalars(select(SyncState).where(
            or_(*[SyncState.key.startswith(p) for p in f.prefixes]))).all()
        future = [r.expires_at for r in rows if r.expires_at and r.expires_at > now]
        f.cached_until = max(future) if future else None
        done = [r.last_success_at for r in rows if r.last_success_at]
        f.last_success = max(done) if done else None
        f.errors = [r.last_error for r in rows if r.last_error]
    return out


def pressed_message(db: Session, kind: str) -> str:
    """Flash text after Sync Now: what will and won't happen."""
    now = utcnow()
    fs = feeds(db, kind, now)
    cached = [f for f in fs if f.cached_until]
    what = "Contracts" if kind == "contracts" else "Skills, jobs and blueprints"
    if not cached:
        return f"{what}: checking ESI now. Fresh data should appear within a minute."
    parts = [f"{f.label.lower()} {fmt_delta(f.cached_until, now)}" for f in cached]
    fresh = [f.label.lower() for f in fs if not f.cached_until]
    msg = (f"{what}: checking ESI now. ESI is still serving its cached copy, so new data "
           f"arrives when that refreshes: {', '.join(parts)}.")
    if fresh:
        msg += f" ({', '.join(fresh).capitalize()} pulled fresh.)"
    return msg


@dataclass
class Expected:
    feed: str
    who: str
    count: int
    per_hour_each: float
    note: str = ""

    @property
    def per_hour(self) -> float:
        return self.count * self.per_hour_each


def _per_hour(poll: timedelta, cache: timedelta) -> float:
    """Most network calls one poller makes per hour: it never asks before ESI's cache
    expires, so the slower of the two clocks wins."""
    return 3600 / max(poll.total_seconds(), cache.total_seconds())


def expected_per_hour(db: Session) -> list[Expected]:
    """Upper bound on ESI calls per hour with the current characters and settings.
    Real numbers are lower: unchanged data comes back as a cheap 304."""
    from app.models import Character
    from app.services.characters import CHAR_BPS, CHAR_JOBS, SKILLS
    s = get_settings()
    chars = db.scalars(select(Character)).all()

    def n(scope: str, toggle: str) -> int:
        return sum(1 for c in chars if c.token and c.token.is_valid
                   and scope in c.token.scope_set and getattr(c, f"track_{toggle}"))

    skills_poll = timedelta(minutes=s.industry_skills_poll_minutes)
    jobs_poll = timedelta(minutes=s.industry_jobs_poll_minutes)
    bps_poll = timedelta(minutes=s.industry_blueprints_poll_minutes)
    rows = [
        Expected("Corp contracts", "poller", 1,
                 _per_hour(timedelta(seconds=s.contract_poll_seconds), timedelta(minutes=5)),
                 "checked every minute, but ESI only refreshes every 5"),
        Expected("Skills + queue", "per character", n(SKILLS, "skills"),
                 2 * _per_hour(skills_poll, timedelta(minutes=2))),
        Expected("Attributes, implants, clones", "per character", n(SKILLS, "skills"),
                 3 * _per_hour(skills_poll, timedelta(minutes=2)),
                 "with the skills sync; implants and clones only once re-linked"),
        Expected("Industry jobs", "per character", n(CHAR_JOBS, "industry"),
                 _per_hour(jobs_poll, timedelta(minutes=5))),
        Expected("Blueprints", "per character", n(CHAR_BPS, "industry"),
                 _per_hour(bps_poll, timedelta(hours=1)), "+1 per extra page of 1000"),
        Expected("Assets", "per character", n("esi-assets.read_assets.v1", "assets"),
                 _per_hour(timedelta(minutes=s.assets_poll_minutes), timedelta(hours=1)),
                 "+1 per extra page of 1000"),
        Expected("Corp jobs, blueprints + assets", "director",
                 1 if any(c.token and c.token.is_valid and
                          "esi-industry.read_corporation_jobs.v1" in c.token.scope_set
                          for c in chars) else 0,
                 _per_hour(jobs_poll, timedelta(minutes=5)) + _per_hour(bps_poll, timedelta(hours=1))
                 + _per_hour(timedelta(minutes=s.assets_poll_minutes), timedelta(hours=1)),
                 "+1 per extra page of 1000 assets"),
    ]
    return [r for r in rows if r.count]
