"""Collateral: what you're responsible for now, and everything you've hauled.

Couriers: a contract's collateral counts from when it's accepted until it's delivered (or
fails). Bulk jobs: the goods' Jita buy value from when the corp accepts the contracts until
you mark the job delivered; once you set a return collateral, that replaces the estimate.
Values are the ones stored at the time, so later price moves don't rewrite history.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import BulkJob, Contract, Customer
from app.models.couriers import FINISHED_EX
from app.services.customers import FINISHED

OPEN_JOB = ("new", "accepted", "hauling")


def _job_value(job: BulkJob) -> float:
    """What a delivered job counts as hauled: your return collateral if you set one,
    else the goods' value."""
    return job.return_collateral_isk or job.value_isk


def _job_held_since(job: BulkJob) -> datetime | None:
    """When the goods came into our hands: the first accepted contract."""
    times = [c.date_completed or c.date_accepted for c in job.contracts
             if c.status in FINISHED_EX and (c.date_completed or c.date_accepted)]
    return min(times) if times else None


@dataclass
class Exposure:
    now: float = 0.0
    couriers: int = 0
    courier_isk: float = 0.0
    jobs: int = 0
    job_isk: float = 0.0
    hauled_month: float = 0.0
    hauled_all: float = 0.0
    daily: list[dict] = field(default_factory=list)   # last 30 days, for the chart


def summary(db: Session, now: datetime, days: int = 30) -> Exposure:
    out = Exposure()
    out.couriers, out.courier_isk = db.execute(
        select(func.count(), func.coalesce(func.sum(Contract.collateral_isk), 0)).where(
            Contract.type == "courier", Contract.status == "in_progress")).one()
    open_jobs = db.scalars(select(BulkJob).where(BulkJob.status.in_(OPEN_JOB))).all()
    held = [j for j in open_jobs if j.exposure_isk]
    out.jobs, out.job_isk = len(held), sum(j.exposure_isk for j in held)
    out.now = (out.courier_isk or 0) + out.job_isk

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    courier_done = select(func.coalesce(func.sum(Contract.collateral_isk), 0)).where(
        Contract.type == "courier", Contract.status.in_(FINISHED))
    out.hauled_all = db.scalar(courier_done) or 0
    out.hauled_month = db.scalar(courier_done.where(Contract.date_completed >= month_start)) or 0
    for j in db.scalars(select(BulkJob).where(BulkJob.status == "delivered")):
        v = _job_value(j)
        out.hauled_all += v
        if j.delivered_at and j.delivered_at >= month_start:
            out.hauled_month += v
    out.daily = daily(db, now, days)
    return out


def daily(db: Session, now: datetime, days: int = 30) -> list[dict]:
    """Exposure at the end of each of the last `days` days (today: now)."""
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    spans: list[tuple[datetime, datetime | None, float]] = []
    for c in db.scalars(select(Contract).where(
            Contract.type == "courier", Contract.date_accepted.is_not(None),
            (Contract.date_completed.is_(None)) | (Contract.date_completed >= start))):
        if c.collateral_isk:
            end = c.date_completed if c.status not in ("in_progress",) else None
            spans.append((c.date_accepted, end, c.collateral_isk))
    for j in db.scalars(select(BulkJob)):
        since = _job_held_since(j)
        if since is None:
            continue
        done = j.status in ("delivered", "closed")
        end = j.delivered_at if done else None
        if end is not None and end < start:
            continue
        # Open: what's in our hands now; delivered: what it counted as when hauled.
        spans.append((since, end, _job_value(j) if done else j.exposure_isk))
    out = []
    for i in range(days):
        day_end = min(start + timedelta(days=i + 1), now)
        isk = sum(v for a, b, v in spans if a <= day_end and (b is None or b > day_end))
        out.append({"day": day_end, "isk": isk})
    peak = max((d["isk"] for d in out), default=0) or 1
    for d in out:
        d["pct"] = round(100 * d["isk"] / peak)
    return out


def per_customer(db: Session) -> dict[int, dict]:
    """issuer_id -> {exposure, hauled, bulk_jobs, bulk_m3} including bulk jobs."""
    out: dict[int, dict] = {}

    def row(cid):
        return out.setdefault(cid, {"exposure": 0.0, "hauled": 0.0, "bulk_jobs": 0,
                                    "bulk_m3": 0.0, "bulk_earned": 0.0})
    for cid, status, coll in db.execute(select(Contract.issuer_id, Contract.status,
                                               Contract.collateral_isk).where(
            Contract.type == "courier", Contract.status.in_(("in_progress",) + FINISHED))):
        r = row(cid)
        if status == "in_progress":
            r["exposure"] += coll or 0
        else:
            r["hauled"] += coll or 0
    for j in db.scalars(select(BulkJob)):
        r = row(j.issuer_id)
        r["bulk_jobs"] += 1
        r["bulk_m3"] += j.m3
        if j.status in OPEN_JOB:
            r["exposure"] += j.exposure_isk
        elif j.status == "delivered":
            r["hauled"] += _job_value(j)
            r["bulk_earned"] += j.earned_isk
    return out


def top_exposure(db: Session, limit: int = 3) -> list[tuple[Customer, float]]:
    rows = [(cid, r["exposure"]) for cid, r in per_customer(db).items() if r["exposure"] > 0]
    rows.sort(key=lambda x: -x[1])
    return [(db.get(Customer, cid), v) for cid, v in rows[:limit]]
