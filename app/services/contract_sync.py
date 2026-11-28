"""Poll corporation contracts from ESI, store courier contracts, emit events + alerts."""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope, utcnow
from app.esi import client as esi
from app.esi import resolver
from app.models import Contract, ContractEvent, Corporation, Customer, Location, SyncState
from app.services.customers import FINISHED
from app.services.notifier import cancel_for_subject, enqueue
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


def _line(c: Contract) -> str:
    """Compact one-liner used in digest DMs."""
    a = (c.start_location.system_name if c.start_location else None) or "?"
    b = (c.end_location.system_name if c.end_location else None) or "?"
    who = c.customer.name if c.customer and c.customer.name else str(c.issuer_id)
    return f"{a} → {b} · {fmt_m3(c.volume_m3)} · {fmt_isk(c.reward_isk)} ISK · {who}"


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


def _alert(db: Session, c: Contract, kind: str, title: str) -> None:
    enqueue(db, ref=f"contract:{c.contract_id}", kind=kind, title=title, body=_summary(c),
            url=f"{get_settings().base_url}/contracts/{c.contract_id}",
            line=_line(c), subject_id=c.issuer_id)


def _queue_alerts(db: Session, r: ApplyResult) -> None:
    c = r.contract
    if c.customer and c.customer.silenced:
        return  # muted or blocked issuer: tracked, never pinged
    if r.is_new and c.status == "outstanding" and (
            c.date_expired is None or c.date_expired > utcnow()):
        _alert(db, c, "new_contract", "🚚 New courier contract")
    for fld, _old, new in r.changes:
        if fld == "status" and new in ALERT_STATUS_CHANGES:
            title, kind = ALERT_STATUS_CHANGES[new]
            _alert(db, c, f"status_{kind}", title)


def detect_spam(db: Session, issuer_ids: set[int]) -> list[Customer]:
    """Auto-mute issuers who posted more than spam_contracts_per_hour contracts
    (by EVE issue time) in the last hour. Sends one warning DM per issuer per hour."""
    s = get_settings()
    since = utcnow() - timedelta(hours=1)
    flagged = []
    for issuer_id in issuer_ids:
        cust = db.get(Customer, issuer_id)
        if cust is None or cust.silenced:
            continue
        recent = db.scalar(select(func.count()).select_from(Contract).where(
            Contract.issuer_id == issuer_id, Contract.date_issued >= since))
        if recent <= s.spam_contracts_per_hour:
            continue
        who = cust.name or str(issuer_id)
        cust.muted = True
        cust.flag_reason = f"Auto-muted: {recent} contracts within an hour"
        cust.flagged_at = utcnow()
        dropped = cancel_for_subject(db, issuer_id)
        log.warning("Auto-muted %s (%s): %d contracts in the last hour", who, issuer_id, recent)
        enqueue(db, ref=f"spam:{issuer_id}:{utcnow():%Y%m%d%H}", kind="spam_detected",
                title="⚠️ Possible contract spam — issuer auto-muted",
                body=(f"**{who}** posted **{recent}** courier contracts in the last hour "
                      f"(limit {s.spam_contracts_per_hour}).\n"
                      f"They're muted: contracts are still tracked, but you won't be pinged "
                      f"about them. {dropped} pending alert(s) were dropped.\n\n"
                      f"Review on their customer page to unmute or block. "
                      f"Reject the contracts in-game if they're junk."),
                url=f"{s.base_url}/customers/{issuer_id}")
        flagged.append(cust)
    return flagged


def queue_deadline_alerts(db: Session) -> None:
    """Warn about outstanding contracts about to expire and accepted ones nearly due."""
    s = get_settings()
    horizon = utcnow() + timedelta(hours=s.expiry_warning_hours)
    now = utcnow()
    open_contracts = db.scalars(
        select(Contract).where(Contract.status.in_(["outstanding", "in_progress"]))).all()
    for c in open_contracts:
        if c.customer and c.customer.silenced:
            continue
        if c.status == "outstanding" and c.date_expired and now < c.date_expired <= horizon:
            _alert(db, c, "expiring", "⏳ Unaccepted contract expiring soon")
        if c.status == "in_progress" and c.deadline and now < c.deadline <= horizon:
            _alert(db, c, "delivery_due", "⏰ Delivery deadline approaching")


# ---------------------------------------------------------------- sync
#
# Shape of a sync (see "Locking rules" in app/db.py):
#   1. network: token + contracts from ESI
#   2. network: names and station/structure names for those contracts (cache tables only)
#   3. one quick transaction, no awaits: store contracts, mute spammers, queue alerts
# Step 3 is all-or-nothing, so a failure never leaves contracts stored without their
# alerts, and nothing holds the database while waiting on CCP.


def courier_rows(corp_id: int, data: list[dict]) -> list[dict]:
    return [r for r in data if r.get("type") == "courier" and r.get("assignee_id") == corp_id]


def quiet_issuers(db: Session, corp_id: int, rows: list[dict]) -> set[int]:
    """Issuers not worth location lookups: already muted/blocked, or about to be
    auto-muted by this batch (same rule as detect_spam, worked out before storing)."""
    limit = get_settings().spam_contracts_per_hour
    since = utcnow() - timedelta(hours=1)
    known = set(db.scalars(select(Contract.contract_id).where(
        Contract.corporation_id == corp_id)))
    new_recent: dict[int, int] = {}
    for r in rows:
        issued = _dt(r.get("date_issued"))
        if r["contract_id"] not in known and issued and issued >= since:
            new_recent[r["issuer_id"]] = new_recent.get(r["issuer_id"], 0) + 1
    quiet: set[int] = set()
    for issuer_id in {r["issuer_id"] for r in rows}:
        cust = db.get(Customer, issuer_id)
        if cust is not None and cust.silenced:
            quiet.add(issuer_id)
        elif issuer_id in new_recent:
            stored = db.scalar(select(func.count()).select_from(Contract).where(
                Contract.issuer_id == issuer_id, Contract.date_issued >= since))
            if stored + new_recent[issuer_id] > limit:
                quiet.add(issuer_id)
    return quiet


async def _prefetch(db: Session, corp_id: int, rows: list[dict], token: str) -> dict:
    """All ESI lookups the batch needs, before anything is stored. Only touches the
    name/location caches, which are safe to keep even if the rest fails."""
    ids: set[int] = set()
    for r in rows:
        ids |= {r["issuer_id"], r.get("issuer_corporation_id") or 0, r.get("acceptor_id") or 0}
    # Customer + corp names: one batched ESI call per 1000 unknown IDs.
    names = await resolver.resolve_names(db, ids)
    # Station/structure names: capped per sync, and never for muted/blocked issuers or a
    # spam flood, so junk contracts can't fan out into ESI calls.
    quiet = quiet_issuers(db, corp_id, rows)
    budget = resolver.LookupBudget(get_settings().max_location_lookups_per_sync)
    for r in rows:
        if r["issuer_id"] in quiet:
            continue
        for loc_id in (r.get("start_location_id"), r.get("end_location_id")):
            await resolver.resolve_location(db, loc_id, token, budget,
                                            source=f"contract {r.get('contract_id')}")
    db.commit()
    return names


def _apply_names(db: Session, results: list[ApplyResult], names: dict) -> None:
    for r in results:
        cust = r.contract.customer or db.get(Customer, r.contract.issuer_id)
        if cust:
            if r.contract.issuer_id in names:
                cust.name = names[r.contract.issuer_id].name
            cust.corporation_id = r.contract.issuer_corp_id or cust.corporation_id
            if cust.corporation_id in names:
                cust.corporation_name = names[cust.corporation_id].name
    db.flush()


def store_batch(db: Session, corp_id: int, rows: list[dict], names: dict) -> list[ApplyResult]:
    """Step 3: synchronous on purpose (no awaits), so it's one short transaction."""
    results = [r for raw in rows if (r := apply_contract(db, corp_id, raw))]
    db.flush()
    _apply_names(db, results, names)
    detect_spam(db, {r.contract.issuer_id for r in results if r.is_new})
    for r in results:
        db.refresh(r.contract)
        _queue_alerts(db, r)
    return results


async def sync_corporation_contracts() -> None:
    with session_scope() as db:
        corp = db.scalar(select(Corporation).where(Corporation.is_home.is_(True)))
        if corp is None or corp.sync_character_id is None:
            log.debug("No tracked corporation yet — log in first.")
            return
        corp_id = corp.corporation_id
        key = f"corp_contracts:{corp_id}"
        try:
            token = await esi.access_token(db, corp.sync_character_id)
            res = await esi.get_cached_paged(db, key, f"/corporations/{corp_id}/contracts",
                                             token=token)
        except (esi.EsiError, esi.NoValidToken) as e:
            log.warning("Contract sync failed: %s", e)
            db.commit()  # persist sync_state.last_error
            return

        try:
            if not res.not_modified:
                # Hold the new ETag back until the contracts are stored, so a crash in
                # between makes the next run refetch instead of seeing "not modified".
                state = db.get(SyncState, key)
                etag, expires = state.etag, state.expires_at
                state.etag, state.expires_at = None, None
                rows = courier_rows(corp_id, res.data)
                names = await _prefetch(db, corp_id, rows, token)

                results = store_batch(db, corp_id, rows, names)
                state = db.get(SyncState, key)
                state.etag, state.expires_at = etag, expires
                log.info("Contracts synced: %d courier rows, %d new, %d changed",
                         len(results), sum(r.is_new for r in results),
                         sum(bool(r.changes) for r in results))
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
