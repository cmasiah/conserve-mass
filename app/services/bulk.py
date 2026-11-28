"""Bulk hauling jobs: item-exchange contracts assigned to the corp ("haul all this for me").

A requester posts many contracts at once, often 0 ISK, from several pickup systems. They're
grouped into one job (same issuer, issued within GROUP_WINDOW of each other), their items
are stored (so nothing is lost after the contracts close), the goods are valued at Jita
buy, and a destination hub is chosen: the nearest of the configured hubs, overridable.

Notes on EVE's side: accepting an item exchange completes it at once, so ESI shows the
contract "finished" as soon as the corp accepts. Here that means "the goods are in our
hands"; delivery is tracked on the job.
"""
import logging
import math
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope, utcnow
from app.esi import client as esi
from app.esi import resolver
from app.models import (
    BulkJob, Contract, ContractItem, Customer, MapSystem, Setting,
)
from app.models.couriers import FINISHED_EX
from app.services import prices
from app.services.notifier import enqueue
from app.utils import fmt_isk, fmt_m3

log = logging.getLogger(__name__)

GROUP_WINDOW = timedelta(hours=2)
HUBS_KEY = "bulk:hubs"
DEFAULT_HUBS = ("AC2E-3", "C-N4OD")          # first = where the JFs are based
OPEN_JOB = ("new", "accepted", "hauling")
ITEM_FETCH_PER_SYNC = 60                     # one ESI call per contract's items


# ------------------------------------------------------------------ settings

def hubs(db: Session) -> list[str]:
    row = db.get(Setting, HUBS_KEY)
    names = [n.strip() for n in (row.value if row else "").split(",") if n.strip()]
    return names or list(DEFAULT_HUBS)


def hub_systems(db: Session) -> list[MapSystem]:
    out = []
    for name in hubs(db):
        s = db.scalar(select(MapSystem).where(MapSystem.name == name))
        if s:
            out.append(s)
    return out


# ------------------------------------------------------------------ grouping

def assign_jobs(db: Session, contracts: list[Contract]) -> list[tuple[BulkJob, bool]]:
    """Put each bulk contract without a job into one: an open job from the same issuer
    whose contracts were issued within GROUP_WINDOW, else a new job.
    Returns [(job, is_new_job)] for jobs that got contracts."""
    touched: dict[int, tuple[BulkJob, bool]] = {}
    for c in sorted(contracts, key=lambda c: c.date_issued):
        if c.job_id or not c.is_bulk:
            continue
        job = db.scalar(select(BulkJob).where(
            BulkJob.issuer_id == c.issuer_id, BulkJob.corporation_id == c.corporation_id,
            BulkJob.status.in_(OPEN_JOB),
            BulkJob.first_issued_at <= c.date_issued + GROUP_WINDOW,
            BulkJob.last_issued_at >= c.date_issued - GROUP_WINDOW,
        ).order_by(BulkJob.last_issued_at.desc()))
        new = job is None
        if new:
            job = BulkJob(issuer_id=c.issuer_id, corporation_id=c.corporation_id,
                          first_issued_at=c.date_issued, last_issued_at=c.date_issued,
                          status="new", created_at=utcnow())
            db.add(job)
            db.flush()
        job.first_issued_at = min(job.first_issued_at, c.date_issued)
        job.last_issued_at = max(job.last_issued_at, c.date_issued)
        c.job_id = job.id
        prev = touched.get(job.id)
        touched[job.id] = (job, new or (prev[1] if prev else False))
    db.flush()
    for job, _new in touched.values():
        db.refresh(job)
        update_status(job)
    return list(touched.values())


def update_status(job: BulkJob) -> None:
    """new: nothing accepted yet · accepted: some or all goods in our hands. Delivered and
    closed are set by you (Batch B watches the return contract)."""
    if job.status in ("delivered", "closed", "hauling"):
        return
    if any(c.status in FINISHED_EX for c in job.contracts):
        job.status = "accepted"


# ------------------------------------------------------------------ destination

@dataclass
class Pickup:
    system: MapSystem | None
    location_ids: set[int] = field(default_factory=set)
    contracts: list[Contract] = field(default_factory=list)
    m3: float = 0.0
    value: float = 0.0
    hub_ly: dict[str, float] = field(default_factory=dict)   # hub name -> straight LY
    nearest: str | None = None

    @property
    def name(self) -> str:
        return self.system.name if self.system else "Unknown system"


def _ly(a: MapSystem, b: MapSystem) -> float:
    from app.services.jump import LY_M
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)) / LY_M


def pickups(db: Session, job: BulkJob) -> list[Pickup]:
    """The job's contracts by pickup system, with the straight-line distance to each hub."""
    hs = hub_systems(db)
    by_sys: dict[int | None, Pickup] = {}
    for c in job.contracts:
        loc = c.start_location
        sid = loc.system_id if loc else None
        p = by_sys.get(sid)
        if p is None:
            p = by_sys[sid] = Pickup(db.get(MapSystem, sid) if sid else None)
        p.contracts.append(c)
        if c.start_location_id:
            p.location_ids.add(c.start_location_id)
        p.m3 += c.volume_m3 or 0
        p.value += c.value_isk or 0
    for p in by_sys.values():
        if p.system:
            p.hub_ly = {h.name: _ly(p.system, h) for h in hs}
            if p.hub_ly:
                p.nearest = min(p.hub_ly, key=p.hub_ly.get)
    return sorted(by_sys.values(), key=lambda p: (-p.m3, p.name))


def nearest_hub(db: Session, job: BulkJob) -> MapSystem | None:
    """The hub closest to the job's cargo: straight-line LY weighted by each pickup's m3
    (fewer, shorter jumps for most of the goods)."""
    ps = [p for p in pickups(db, job) if p.hub_ly]
    if not ps:
        return None
    total = {h: sum(p.hub_ly[h] * max(p.m3, 1) for p in ps) for h in ps[0].hub_ly}
    best = min(total, key=total.get)
    return db.scalar(select(MapSystem).where(MapSystem.name == best))


def choose_destination(db: Session, job: BulkJob) -> None:
    if job.dest_override:
        return
    hub = nearest_hub(db, job)
    if hub:
        job.dest_system_id = hub.system_id


# ------------------------------------------------------------------ items and value

def _value_contract(db: Session, c: Contract, price: dict[int, float]) -> None:
    """Fix the goods' value once (Jita buy at the time). Blueprint copies have no market
    price, so they count as 0."""
    if c.value_isk is not None or not c.items:
        return
    total = 0.0
    for it in c.items:
        if not it.is_included:
            continue
        unit = 0.0 if it.is_bpc else price.get(it.type_id, 0.0)
        it.unit_value = unit
        total += unit * it.quantity
    c.value_isk = total


def store_items(db: Session, contract_id: int, rows: list[dict]) -> None:
    for r in rows:
        if db.get(ContractItem, r["record_id"]):
            continue
        qty = r.get("quantity", 1)
        # raw_quantity: -1 = blueprint original, -2 = blueprint copy (one item each).
        db.add(ContractItem(record_id=r["record_id"], contract_id=contract_id,
                            type_id=r["type_id"], quantity=qty if qty > 0 else 1,
                            is_included=r.get("is_included", True),
                            is_bpc=r.get("raw_quantity") == -2))


# ------------------------------------------------------------------ after each sync

def _needs_items(db: Session, corp_id: int) -> list[int]:
    have = select(ContractItem.contract_id).distinct()
    return list(db.scalars(select(Contract.contract_id).where(
        Contract.corporation_id == corp_id, Contract.type == "item_exchange",
        Contract.contract_id.not_in(have)).limit(ITEM_FETCH_PER_SYNC)))


async def after_sync(corp_id: int, token: str) -> None:
    """Fetch items for new bulk contracts, price them, group into jobs, pick the hub, and
    queue notifications. Network calls happen before the short write at the end."""
    with session_scope() as db:
        todo = _needs_items(db, corp_id)
    fetched: dict[int, list[dict]] = {}
    for cid in todo:
        try:
            fetched[cid] = await esi.get(f"/corporations/{corp_id}/contracts/{cid}/items",
                                         token=token) or []
        except esi.EsiError as e:
            log.info("Items for contract %s unavailable: %s", cid, e)
            if getattr(e, "status", None) in (403, 404):
                fetched[cid] = []      # gone or not visible: don't ask every minute
    type_ids = {r["type_id"] for rows in fetched.values() for r in rows}
    if type_ids:
        await prices.ensure(type_ids)
        with session_scope() as db:
            await resolver.resolve_names(db, type_ids)
            db.commit()

    with session_scope() as db:
        for cid, rows in fetched.items():
            store_items(db, cid, rows)
        db.flush()
        bulk = list(db.scalars(select(Contract).where(
            Contract.corporation_id == corp_id, Contract.type == "item_exchange")))
        price = prices.jita_buy(db, {it.type_id for c in bulk for it in c.items})
        for c in bulk:
            _value_contract(db, c, price)
        touched = assign_jobs(db, bulk)
        for job, _new in touched:
            if job.priced_at is None and all(c.value_isk is not None for c in job.contracts):
                job.priced_at = utcnow()
            choose_destination(db, job)
        for job in db.scalars(select(BulkJob).where(BulkJob.status.in_(OPEN_JOB))):
            update_status(job)
        for job, is_new in touched:
            _notify_job(db, job, is_new)
        queue_expiry_alerts(db)
        db.commit()


# ------------------------------------------------------------------ notifications

def job_line(db: Session, job: BulkJob) -> str:
    who = job.customer.name if job.customer and job.customer.name else str(job.issuer_id)
    n = len(job.contracts)
    ps = {c.start_location.system_name for c in job.contracts
          if c.start_location and c.start_location.system_name}
    pay = (f"💰 {fmt_isk(job.upfront_isk)} ISK upfront" if job.price_mode == "upfront"
           else "🧾 price after")
    value = f" · {fmt_isk(job.value_isk)} ISK of goods" if job.value_isk else ""
    return (f"{who}: {n} contract{'s' if n != 1 else ''}, {fmt_m3(job.m3)}, "
            f"{len(ps)} pickup system{'s' if len(ps) != 1 else ''}{value} · {pay}")


def _notify_job(db: Session, job: BulkJob, is_new: bool) -> None:
    cust = db.get(Customer, job.issuer_id)
    if cust and cust.silenced:
        return
    dest = db.get(MapSystem, job.dest_system_id) if job.dest_system_id else None
    body = job_line(db, job) + (f"\n**Nearest hub:** {dest.name}" if dest else "")
    url = f"{get_settings().base_url}/contracts/bulk/{job.id}"
    # One message per job; contracts that join later (same issuer, within 2h) update the
    # count via a second ref so a growing job is noticed once more, not per contract.
    ref = f"bulkjob:{job.id}" if is_new else f"bulkjob:{job.id}:{len(job.contracts)}"
    enqueue(db, ref=ref, kind="bulk_job",
            title=("📦 Bulk job assigned to the corp" if is_new
                   else "📦 Bulk job grew"),
            body=body, url=url, line=job_line(db, job), subject_id=job.issuer_id)


def queue_expiry_alerts(db: Session) -> None:
    """One heads-up per job when unaccepted contracts expire within the warning window."""
    horizon = utcnow() + timedelta(hours=get_settings().expiry_warning_hours)
    now = utcnow()
    for job in db.scalars(select(BulkJob).where(BulkJob.status.in_(("new", "accepted")))):
        waiting = [c for c in job.contracts if c.status == "outstanding" and c.date_expired]
        soon = [c for c in waiting if now < c.date_expired <= horizon]
        if not soon:
            continue
        first = min(c.date_expired for c in soon)
        enqueue(db, ref=f"bulkexp:{job.id}:{first:%Y%m%d}", kind="bulk_expiring",
                title="⏳ Bulk job contracts expiring soon",
                body=f"{job_line(db, job)}\n**{len(soon)}** not accepted yet; the first "
                     f"expires {first:%Y-%m-%d %H:%M} EVE.",
                url=f"{get_settings().base_url}/contracts/bulk/{job.id}",
                subject_id=job.issuer_id)


# ------------------------------------------------------------------ stockpile exclusion

def reserved_quantities(db: Session) -> dict[int, int]:
    """Goods from open bulk jobs that are in our hands: someone else's, so Materials and
    Stockpile must not count them. type_id -> quantity."""
    out: dict[int, int] = {}
    rows = db.execute(select(ContractItem.type_id, ContractItem.quantity).join(
        Contract, Contract.contract_id == ContractItem.contract_id).join(
        BulkJob, BulkJob.id == Contract.job_id).where(
        BulkJob.status.in_(OPEN_JOB), Contract.status.in_(FINISHED_EX),
        ContractItem.is_included.is_(True)))
    for tid, qty in rows:
        out[tid] = out.get(tid, 0) + int(qty or 0)
    return out
