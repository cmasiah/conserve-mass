"""What 'Sync now' does and when data refreshes on its own.

Two clocks decide when new data shows up:
  * our schedule: how often the app asks ESI (Settings, defaults below);
  * ESI's cache: how long CCP serves the same answer. Asking again sooner returns the
    same data, so neither the schedule nor Sync now can beat it.
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
                     "5 min", ("corp_contracts:",))]
    return [
        Feed("Skills & queues", "industry_skills",
             timedelta(minutes=s.industry_skills_poll_minutes), "2 min", ("skills:", "skillqueue:")),
        Feed("Industry jobs", "industry_jobs",
             timedelta(minutes=s.industry_jobs_poll_minutes), "5 min", ("jobs:",)),
        Feed("Blueprints", "industry_blueprints",
             timedelta(minutes=s.industry_blueprints_poll_minutes), "1 hour", ("bps:", "divisions:")),
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
    """Flash text after Sync now: what will and won't happen."""
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
