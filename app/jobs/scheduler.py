"""In-process scheduler (APScheduler). Single container, single worker — keep it that way."""
import asyncio
import functools
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.db import is_locked_error
from app.services.assets import sync_assets
from app.services.backup import run_backup
from app.services.contract_sync import sync_corporation_contracts
from app.services.industry import check_job_alerts, sync_blueprints, sync_jobs, sync_skills
from app.services.notifier import dispatch_pending
from app.services.version import version_job

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(job_defaults={"coalesce": True, "max_instances": 1,
                                           "misfire_grace_time": 120})


def _guarded(fn):
    """A busy database is a passing condition, not a crash: log one line and let the
    next scheduled run pick the work up, instead of a traceback every time."""
    @functools.wraps(fn)
    async def run() -> None:
        try:
            await fn()
        except OperationalError as e:
            if not is_locked_error(e):
                raise
            log.warning("%s skipped: database busy (%s); retrying next run", fn.__name__, e.orig)
    return run


async def _backup_job() -> None:
    await asyncio.to_thread(run_backup)


def start() -> None:
    s = get_settings()
    scheduler.add_job(_guarded(sync_corporation_contracts), IntervalTrigger(seconds=s.contract_poll_seconds),
                      id="contract_sync", next_run_time=datetime.now() + timedelta(seconds=10))
    scheduler.add_job(_guarded(dispatch_pending), IntervalTrigger(seconds=20), id="notify_dispatch")
    scheduler.add_job(_backup_job, CronTrigger(hour=4, minute=17), id="db_backup")
    scheduler.add_job(_guarded(version_job), IntervalTrigger(minutes=30), id="version_check",
                      next_run_time=datetime.now() + timedelta(seconds=30))
    # Industry: staggered first runs so a restart doesn't fire everything at once.
    scheduler.add_job(_guarded(sync_skills), IntervalTrigger(minutes=s.industry_skills_poll_minutes),
                      id="industry_skills", next_run_time=datetime.now() + timedelta(seconds=45))
    scheduler.add_job(_guarded(sync_jobs), IntervalTrigger(minutes=s.industry_jobs_poll_minutes),
                      id="industry_jobs", next_run_time=datetime.now() + timedelta(seconds=75))
    scheduler.add_job(_guarded(sync_blueprints),
                      IntervalTrigger(minutes=s.industry_blueprints_poll_minutes),
                      id="industry_blueprints",
                      next_run_time=datetime.now() + timedelta(seconds=105))
    scheduler.add_job(_guarded(sync_assets), IntervalTrigger(minutes=s.assets_poll_minutes),
                      id="industry_assets", next_run_time=datetime.now() + timedelta(seconds=135))
    # DB-only: catches jobs finishing between ESI syncs, no ESI calls.
    scheduler.add_job(_guarded(check_job_alerts), IntervalTrigger(minutes=1), id="industry_job_alerts")
    scheduler.start()
    log.info("Scheduler started (contract poll every %ss)", s.contract_poll_seconds)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def trigger_industry_now() -> None:
    for job_id in ("industry_skills", "industry_jobs", "industry_blueprints", "industry_assets"):
        job = scheduler.get_job(job_id)
        if job:
            job.modify(next_run_time=datetime.now())


def trigger_sync_now() -> None:
    job = scheduler.get_job("contract_sync")
    if job:
        job.modify(next_run_time=datetime.now())
