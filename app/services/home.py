"""Home page summary, built only from data the app already stores (no ESI calls).

Each section is a small function so widgets can be added, removed or reordered
without touching the others.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import (
    Character, Contract, Customer, EntityName, EsiToken, Notification, SyncState,
)
from app.models.industry import IndustryJob, SkillQueue
from app.services import industry
from app.services.customers import FINISHED

SLOT_CATS = ("manufacturing", "science", "reactions")


@dataclass
class Attention:
    level: str          # "bad" | "warn" | "info"
    text: str
    href: str


@dataclass
class HaulingSummary:
    outstanding: int = 0
    in_transit: int = 0
    collateral_at_risk: float = 0
    delivered_30d: int = 0
    reward_30d: float = 0
    expiring_24h: int = 0


@dataclass
class IndustrySummary:
    characters: int = 0
    free: dict[str, int] = field(default_factory=dict)
    total: dict[str, int] = field(default_factory=dict)
    active_jobs: int = 0
    ready_jobs: int = 0
    next_end: datetime | None = None


def _not_blocked():
    """Couriers from issuers that aren't blocked (bulk jobs have their own summary)."""
    return Contract.issuer_id.not_in(
        select(Customer.character_id).where(Customer.blocked.is_(True))) & (
        Contract.type == "courier")


def hauling_summary(db: Session, now: datetime) -> HaulingSummary:
    nb = _not_blocked()
    out = HaulingSummary()
    out.outstanding = db.scalar(select(func.count()).select_from(Contract).where(
        Contract.status == "outstanding", nb)) or 0
    out.in_transit, out.collateral_at_risk = db.execute(
        select(func.count(), func.coalesce(func.sum(Contract.collateral_isk), 0))
        .where(Contract.status == "in_progress", nb)).one()
    out.delivered_30d, out.reward_30d = db.execute(
        select(func.count(), func.coalesce(func.sum(Contract.reward_isk), 0))
        .where(Contract.status.in_(FINISHED), Contract.type == "courier",
               Contract.date_completed >= now - timedelta(days=30))).one()
    out.expiring_24h = db.scalar(select(func.count()).select_from(Contract).where(
        Contract.status == "outstanding", nb,
        Contract.date_expired.is_not(None),
        Contract.date_expired <= now + timedelta(days=1))) or 0
    return out


def industry_summary(db: Session, now: datetime) -> IndustrySummary:
    out = IndustrySummary(free={c: 0 for c in SLOT_CATS}, total={c: 0 for c in SLOT_CATS})
    for row in industry.character_overview(db):
        if not row.has_skills:
            continue
        out.characters += 1
        for c in SLOT_CATS:
            out.total[c] += row.capacity.get(c, 0)
            out.free[c] += row.free(c)
    jobs = db.scalars(select(IndustryJob)).all()
    for j in jobs:
        if j.status == "ready" or (j.status == "active" and j.end_date <= now):
            out.ready_jobs += 1
        elif j.status == "active":
            out.active_jobs += 1
            if out.next_end is None or j.end_date < out.next_end:
                out.next_end = j.end_date
    return out


def attention(db: Session, now: datetime, *, bot_configured: bool, bot_ready: bool,
              update_available: bool, latest: str | None) -> list[Attention]:
    items: list[Attention] = []
    for c in db.scalars(select(Customer).where(
            Customer.muted.is_(True), Customer.flagged_at.is_not(None),
            Customer.flagged_at >= now - timedelta(days=1))):
        items.append(Attention("bad", f"{c.name or c.character_id} was auto-muted: {c.flag_reason}",
                               f"/customers/{c.character_id}"))
    for t in db.scalars(select(EsiToken).where(EsiToken.is_valid.is_(False))):
        name = t.character.name if t.character else t.character_id
        items.append(Attention("bad", f"ESI login for {name} needs re-linking", "/characters"))
    # One line for all failing syncs (role-parked "denied:" markers are expected, not errors).
    failing = db.scalars(select(SyncState).where(
        SyncState.last_error.is_not(None), ~SyncState.key.startswith("denied:"))
        .order_by(SyncState.last_attempt_at.desc())).all()
    if failing:
        newest = failing[0]
        more = f" (+{len(failing) - 1} more)" if len(failing) > 1 else ""
        items.append(Attention("warn", f"Sync error: {newest.key}: {newest.last_error[:90]}{more}",
                               "/system"))
    if not bot_configured:
        items.append(Attention("warn", "Discord isn't configured; alerts are queuing", "/system"))
    elif not bot_ready:
        pending = db.scalar(select(func.count()).select_from(Notification)
                            .where(Notification.status == "pending")) or 0
        items.append(Attention("warn", f"Discord bot not connected ({pending} alerts pending)",
                               "/system"))
    if update_available:
        items.append(Attention("info", f"Update available: {latest}", "/system"))
    return items


# ---------------------------------------------------------------- widgets

ACTIVITY_LABEL = {1: "Manufacturing", 3: "TE research", 4: "ME research", 5: "Copying",
                  7: "Reverse eng.", 8: "Invention", 9: "Reaction", 11: "Reaction"}


def _names(db: Session, ids) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {e.entity_id: e.name for e in
            db.scalars(select(EntityName).where(EntityName.entity_id.in_(ids)))}


def isk_by_week(db: Session, now: datetime, weeks: int = 8) -> list[dict]:
    """Delivered courier reward per week (weeks start Monday, UTC), oldest first."""
    this_monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    starts = [this_monday - timedelta(weeks=w) for w in range(weeks - 1, -1, -1)]
    buckets = {d: {"start": d, "count": 0, "isk": 0.0} for d in starts}
    for done, reward in db.execute(
            select(Contract.date_completed, Contract.reward_isk).where(
                Contract.status.in_(FINISHED), Contract.type == "courier",
                Contract.date_completed >= starts[0])):
        wk = (done - timedelta(days=done.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if wk in buckets:
            buckets[wk]["count"] += 1
            buckets[wk]["isk"] += reward or 0
    rows = [buckets[d] for d in starts]
    peak = max((r["isk"] for r in rows), default=0) or 1
    for r in rows:
        r["pct"] = round(100 * r["isk"] / peak)
    return rows


def top_customers(db: Session, now: datetime, limit: int = 5) -> list[dict]:
    """Best customers this calendar month (UTC) by delivered reward."""
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    q = (select(Customer.character_id, Customer.name, func.count(),
                func.coalesce(func.sum(Contract.reward_isk), 0).label("isk"))
         .join(Contract, Contract.issuer_id == Customer.character_id)
         .where(Contract.status.in_(FINISHED), Contract.type == "courier",
                Contract.date_completed >= month_start)
         .group_by(Customer.character_id, Customer.name)
         .order_by(func.sum(Contract.reward_isk).desc()).limit(limit))
    return [{"id": cid, "name": name or str(cid), "count": n, "isk": isk}
            for cid, name, n, isk in db.execute(q)]


def deadlines(db: Session, now: datetime, limit: int = 6) -> list[dict]:
    """Soonest due: deliveries for accepted contracts, acceptance windows for open ones."""
    nb = _not_blocked()
    out = []
    for c in db.scalars(select(Contract).where(Contract.status == "in_progress", nb)):
        if c.date_accepted and c.days_to_complete:
            out.append({"c": c, "kind": "deliver", "at": c.date_accepted
                        + timedelta(days=c.days_to_complete)})
    for c in db.scalars(select(Contract).where(Contract.status == "outstanding", nb,
                                               Contract.date_expired.is_not(None))):
        out.append({"c": c, "kind": "accept", "at": c.date_expired})
    out.sort(key=lambda d: d["at"])
    for d in out:
        left = d["at"] - now
        d["level"] = "bad" if left < timedelta(hours=6) else (
            "warn" if left < timedelta(days=1) else "")
    return out[:limit]


def jobs_next_24h(db: Session, now: datetime, limit: int = 10) -> list[dict]:
    jobs = db.scalars(select(IndustryJob).where(
        IndustryJob.status == "active", IndustryJob.end_date > now,
        IndustryJob.end_date <= now + timedelta(days=1)).order_by(IndustryJob.end_date)
        .limit(limit)).all()
    names = _names(db, {j.product_type_id or j.blueprint_type_id for j in jobs}
                   | {j.installer_id for j in jobs})
    return [{"job": j, "end": j.end_date, "runs": j.runs,
             "what": names.get(j.product_type_id or j.blueprint_type_id, "Unknown item"),
             "who": names.get(j.installer_id, str(j.installer_id)),
             "activity": ACTIVITY_LABEL.get(j.activity_id, "Job"),
             "pct": round(100 * (now - j.start_date) / (j.end_date - j.start_date))
             if j.end_date > j.start_date else 100} for j in jobs]


def idle_characters(db: Session, limit: int = 8) -> list[dict]:
    """Characters with free slots, fully idle ones first."""
    out = []
    for row in industry.character_overview(db):
        if not row.has_skills:
            continue
        free = {c: row.free(c) for c in SLOT_CATS}
        if not any(free.values()):
            continue
        out.append({"character": row.character, "free": free,
                    "idle": not any(row.used.values()), "total_free": sum(free.values())})
    out.sort(key=lambda r: (not r["idle"], -r["total_free"], r["character"].name or ""))
    return out[:limit]


def skill_queues(db: Session, now: datetime) -> list[dict]:
    """Queue status per character: empty and paused first, then soonest to run out."""
    rows = db.execute(select(SkillQueue, Character).join(
        Character, Character.character_id == SkillQueue.character_id)
        .where(Character.track_skills.is_(True))).all()
    names = _names(db, {q.current_skill_id for q, _ in rows})
    out = []
    for q, ch in rows:
        if q.length == 0:
            state, level, order = "Empty", "bad", 0
        elif q.queue_end is None:
            state, level, order = "Paused", "warn", 1
        elif q.queue_end - now < timedelta(days=1):
            state, level, order = "Ends soon", "warn", 2
        else:
            state, level, order = "Training", "ok", 3
        out.append({"character": ch, "q": q, "state": state, "level": level, "order": order,
                    "skill": names.get(q.current_skill_id) if q.current_skill_id else None})
    out.sort(key=lambda r: (r["order"], r["q"].queue_end or now, r["character"].name or ""))
    return out


@dataclass
class StockSummary:
    stacks: int = 0
    watched_places: int = 0
    targets: int = 0
    short: list = field(default_factory=list)     # (name, amount to buy), biggest first
    excess: int = 0


def stock_summary(db: Session) -> StockSummary:
    from app.models import Asset, AssetWatch, MaterialTarget
    from app.services.assets import material_lines
    out = StockSummary(
        stacks=db.scalar(select(func.count()).select_from(Asset)) or 0,
        watched_places=db.scalar(select(func.count()).select_from(AssetWatch)) or 0,
        targets=db.scalar(select(func.count()).select_from(MaterialTarget)) or 0)
    if out.targets:
        _sections, every = material_lines(db, "watched")
        out.short = sorted(((ln.name, ln.short, ln.min_qty) for ln in every if ln.short),
                           key=lambda x: -(x[1] / x[2] if x[2] else 0))
        out.excess = sum(1 for ln in every if ln.excess)
    return out


def build(db: Session, **flags) -> dict:
    now = utcnow()
    return {"now": now, "hauling": hauling_summary(db, now),
            "industry": industry_summary(db, now), "attention": attention(db, now, **flags),
            "isk_weeks": isk_by_week(db, now), "top_customers": top_customers(db, now),
            "deadlines": deadlines(db, now, limit=5), "jobs_24h": jobs_next_24h(db, now, limit=6),
            "idle": idle_characters(db), "queues": skill_queues(db, now),
            "stock": stock_summary(db), **_bulk_bits(db, now),
            "linked": db.scalar(select(func.count()).select_from(Character)) or 0}


def _bulk_bits(db: Session, now) -> dict:
    from app.models import BulkJob
    from app.services import exposure
    e = exposure.summary(db, now, days=1)
    return {"exposure_now": e.now, "open_bulk": db.scalar(
        select(func.count()).select_from(BulkJob).where(
            BulkJob.status.in_(("new", "accepted", "hauling")))) or 0}
