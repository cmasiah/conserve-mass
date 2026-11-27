"""In-process scheduler (APScheduler). Single container, single worker — keep it that way."""
import asyncio
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.services.backup import run_backup
from app.services.contract_sync import sync_corporation_contracts
from app.services.notifier import dispatch_pending

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(job_defaults={"coalesce": True, "max_instances": 1,
                                           "misfire_grace_time": 120})


async def _backup_job() -> None:
    await asyncio.to_thread(run_backup)


def start() -> None:
    s = get_settings()
    scheduler.add_job(sync_corporation_contracts, IntervalTrigger(seconds=s.contract_poll_seconds),
                      id="contract_sync", next_run_time=datetime.now() + timedelta(seconds=10))
    scheduler.add_job(dispatch_pending, IntervalTrigger(seconds=20), id="notify_dispatch")
    scheduler.add_job(_backup_job, CronTrigger(hour=4, minute=17), id="db_backup")
    # Future: industry_jobs_sync, skills_sync, assets_sync register here.
    scheduler.start()
    log.info("Scheduler started (contract poll every %ss)", s.contract_poll_seconds)


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def trigger_sync_now() -> None:
    job = scheduler.get_job("contract_sync")
    if job:
        job.modify(next_run_time=datetime.now())
