"""Poll corporation contracts from ESI, store courier contracts, emit events + alerts."""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope, utcnow
from app.esi import client as esi
from app.esi import resolver
from app.models import Contract, ContractEvent, Corporation, Customer, Location, SyncState
from app.services.customers import FINISHED
from app.services.notifier import enqueue
from app.utils import fmt_delta, fmt_isk, fmt_m3

log = logging.getLogger(__name__)

TRACKED_FIELDS = ("status", "acceptor_id", "date_accepted", "date_completed")
ALERT_STATUS_CHANGES = {
    "in_progress": ("📦 Contract accepted", "accepted"),
    "finished": ("✅ Contract delivered", "finished"),
    "failed": ("❌ Contract FAILED", "failed"),
    "cancelled": ("🚫 Contract cancelled by issuer", "cancelled"),
    "deleted": ("🗑️ Contract deleted by issuer", "deleted"),
    "rejected": ("↩️ Contract rejected", "rejected"),
}
AUTO_WORK_STATUS = {"in_progress": "accepted", "finished": "delivered",
                    "finished_contractor": "delivered", "finished_issuer": "delivered"}


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass
class ApplyResult:
    contract: Contract
    is_new: bool
    changes: list[tuple[str, str | None, str | None]] = field(default_factory=list)


def apply_contract(db: Session, corp_id: int, raw: dict) -> ApplyResult | None:
    """Upsert one ESI contract row. Returns None if it's not a courier assigned to us."""
    if raw.get("type") != "courier" or raw.get("assignee_id") != corp_id:
        return None

    now = utcnow()
    issuer_id = raw["issuer_id"]
    with db.no_autoflush:
        # Parent rows must exist before the contract row references them (FKs).
        if db.get(Customer, issuer_id) is None:
            db.add(Customer(character_id=issuer_id,
                            corporation_id=raw.get("issuer_corporation_id"),
                            first_seen_at=_dt(raw.get("date_issued")) or now))
        for loc_id in {raw.get("start_location_id"), raw.get("end_location_id")} - {None, 0}:
            if db.get(Location, loc_id) is None:
                # Placeholder; resolver fills in name/system during _enrich.
                db.add(Location(location_id=loc_id, kind="unknown"))
        c = db.get(Contract, raw["contract_id"])
    db.flush()

    is_new = c is None
    if is_new:
        c = Contract(contract_id=raw["contract_id"], corporation_id=corp_id,
                     first_seen_at=now, work_status="new")
        db.add(c)

    new_values = {
        "issuer_id": issuer_id,
        "issuer_corp_id": raw.get("issuer_corporation_id"),
        "acceptor_id": raw.get("acceptor_id") or None,
        "assignee_id": raw.get("assignee_id"),
        "type": raw["type"],
        "status": raw["status"],
        "title": raw.get("title") or None,
        "start_location_id": raw.get("start_location_id"),
        "end_location_id": raw.get("end_location_id"),
        "volume_m3": raw.get("volume"),
        "reward_isk": raw.get("reward"),
        "collateral_isk": raw.get("collateral"),
        "days_to_complete": raw.get("days_to_complete"),
        "date_issued": _dt(raw.get("date_issued")),
        "date_expired": _dt(raw.get("date_expired")),
        "date_accepted": _dt(raw.get("date_accepted")),
        "date_completed": _dt(raw.get("date_completed")),
    }

    result = ApplyResult(contract=c, is_new=is_new)
    for key, value in new_values.items():
        old = getattr(c, key, None)
        if not is_new and key in TRACKED_FIELDS and old != value:
            result.changes.append((key, None if old is None else str(old),
                                   None if value is None else str(value)))
        setattr(c, key, value)

    for fld, old, new in result.changes:
        db.add(ContractEvent(contract_id=c.contract_id, field=fld, old_value=old,
                             new_value=new, source="esi", observed_at=now))
        if fld == "status" and new in AUTO_WORK_STATUS and c.work_status in (
                "new", "queued", "accepted", "in_transit"):
            c.work_status = AUTO_WORK_STATUS[new]

    if is_new:
        db.add(ContractEvent(contract_id=c.contract_id, field="status", old_value=None,
                             new_value=c.status, source="esi", observed_at=now))
        if c.status in AUTO_WORK_STATUS:
            c.work_status = AUTO_WORK_STATUS[c.status]

    c.last_seen_at = now
    c.raw_json = json.dumps(raw, default=str)
    return result


def _route(c: Contract) -> str:
    a = c.start_location.display if c.start_location else "?"
    b = c.end_location.display if c.end_location else "?"
    return f"{a}\n→ {b}"


def _summary(c: Contract) -> str:
    who = c.customer.name if c.customer and c.customer.name else str(c.issuer_id)
    lines = [
        f"**From:** {who}",
        f"**Route:** {_route(c)}",
        f"**Volume:** {fmt_m3(c.volume_m3)}",
        f"**Reward:** {fmt_isk(c.reward_isk)} ISK · **Collateral:** {fmt_isk(c.collateral_isk)} ISK",
    ]
    if c.status == "outstanding" and c.date_expired:
        lines.append(f"**Expires:** {fmt_delta(c.date_expired)}")
    if c.deadline:
        lines.append(f"**Deliver by:** {fmt_delta(c.deadline)}")
    if c.customer:
        delivered = sum(1 for k in c.customer.contracts if k.status in FINISHED)
        if delivered:
            lines.append(f"🔁 Returning customer — {delivered} delivered contract"
                         f"{'s' if delivered != 1 else ''}")
    if c.title:
        lines.append(f"**Note:** {c.title}")
    return "\n".join(lines)


def _queue_alerts(db: Session, r: ApplyResult) -> None:
    c = r.contract
    url = f"{get_settings().base_url}/contracts/{c.contract_id}"
    ref = f"contract:{c.contract_id}"
    if r.is_new and c.status == "outstanding" and (
            c.date_expired is None or c.date_expired > utcnow()):
        enqueue(db, ref=ref, kind="new_contract", title="🚚 New courier contract",
                body=_summary(c), url=url)
    for fld, _old, new in r.changes:
        if fld == "status" and new in ALERT_STATUS_CHANGES:
            title, kind = ALERT_STATUS_CHANGES[new]
            enqueue(db, ref=ref, kind=f"status_{kind}", title=title, body=_summary(c), url=url)


def queue_deadline_alerts(db: Session) -> None:
    """Warn about outstanding contracts about to expire and accepted ones nearly due."""
    s = get_settings()
    horizon = utcnow() + timedelta(hours=s.expiry_warning_hours)
    now = utcnow()
    open_contracts = db.scalars(
        select(Contract).where(Contract.status.in_(["outstanding", "in_progress"]))).all()
    for c in open_contracts:
        url = f"{s.base_url}/contracts/{c.contract_id}"
        ref = f"contract:{c.contract_id}"
        if c.status == "outstanding" and c.date_expired and now < c.date_expired <= horizon:
            enqueue(db, ref=ref, kind="expiring", title="⏳ Unaccepted contract expiring soon",
                    body=_summary(c), url=url)
        if c.status == "in_progress" and c.deadline and now < c.deadline <= horizon:
            enqueue(db, ref=ref, kind="delivery_due", title="⏰ Delivery deadline approaching",
                    body=_summary(c), url=url)


async def _enrich(db: Session, results: list[ApplyResult], token: str) -> None:
    ids: set[int] = set()
    for r in results:
        ids |= {r.contract.issuer_id, r.contract.issuer_corp_id or 0,
                r.contract.acceptor_id or 0}
    names = await resolver.resolve_names(db, ids)
    for r in results:
        cust = r.contract.customer or db.get(Customer, r.contract.issuer_id)
        if cust:
            if r.contract.issuer_id in names:
                cust.name = names[r.contract.issuer_id].name
            cust.corporation_id = r.contract.issuer_corp_id or cust.corporation_id
            if cust.corporation_id in names:
                cust.corporation_name = names[cust.corporation_id].name
        for loc_id in (r.contract.start_location_id, r.contract.end_location_id):
            await resolver.resolve_location(db, loc_id, token)
    db.flush()


async def sync_corporation_contracts() -> None:
    with session_scope() as db:
        corp = db.scalar(select(Corporation).where(Corporation.is_tracked.is_(True)))
        if corp is None or corp.sync_character_id is None:
            log.debug("No tracked corporation yet — log in first.")
            return
        corp_id = corp.corporation_id
        try:
            token = await esi.access_token(db, corp.sync_character_id)
            res = await esi.get_cached_paged(
                db, f"corp_contracts:{corp_id}", f"/corporations/{corp_id}/contracts",
                token=token)
        except (esi.EsiError, esi.NoValidToken) as e:
            log.warning("Contract sync failed: %s", e)
            db.commit()  # persist sync_state.last_error
            return

        key = f"corp_contracts:{corp_id}"
        try:
            if not res.not_modified:
                results = [r for raw in res.data if (r := apply_contract(db, corp_id, raw))]
                db.flush()
                await _enrich(db, results, token)
                for r in results:
                    db.refresh(r.contract)
                    _queue_alerts(db, r)
                new = sum(r.is_new for r in results)
                changed = sum(bool(r.changes) for r in results)
                log.info("Contracts synced: %d courier rows, %d new, %d changed",
                         len(results), new, changed)
            queue_deadline_alerts(db)
            db.commit()
        except Exception as e:  # noqa: BLE001 — record any processing failure, retry next run
            log.exception("Contract processing failed")
            db.rollback()
            state = db.get(SyncState, key) or SyncState(key=key)
            # Clear cache markers so the next run refetches instead of skipping.
            state.etag, state.expires_at = None, None
            state.last_attempt_at = utcnow()
            state.last_error = f"processing: {type(e).__name__}: {str(e)[:250]}"
            db.add(state)
            db.commit()
