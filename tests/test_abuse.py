"""Flood / spam protections: digests, auto-mute, lookup limits."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from tests.test_contract_sync import CORP, raw_contract


def _n(kind, i=0, line=None):
    return SimpleNamespace(kind=kind, title=f"{kind} {i}", line=line or f"line {i}")


# ---------------------------------------------------------------- digests

def test_hundred_new_contracts_become_one_message():
    from app.services.notifier import plan_messages
    pending = [_n("new_contract", i) for i in range(100)]
    msgs = plan_messages(pending, threshold=3, limit=3)
    assert len(msgs) == 1 and msgs[0].digest and len(msgs[0].items) == 100


def test_small_batches_are_sent_individually_and_capped():
    from app.services.notifier import plan_messages
    pending = [_n("new_contract", i) for i in range(3)] + [_n("test")]
    msgs = plan_messages(pending, threshold=3, limit=3)
    assert [m.digest for m in msgs] == [False, False, False]  # 4th waits for next run


def test_one_off_alerts_are_never_digested():
    from app.services.notifier import plan_messages
    pending = [_n("spam_detected", i) for i in range(5)]
    msgs = plan_messages(pending, threshold=3, limit=10)
    assert len(msgs) == 5 and not any(m.digest for m in msgs)


def test_digest_text_lists_top_items_and_counts_rest():
    from app.services.notifier import DIGEST_LINES, digest_text
    title, body = digest_text("new_contract", [_n("new_contract", i) for i in range(40)])
    assert title == "🚚 40 new courier contracts"
    assert body.count("•") == DIGEST_LINES and "25 more" in body


# ---------------------------------------------------------------- spam detection

def _recent(minutes_ago=5):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat() \
        .replace("+00:00", "Z")


def test_burst_from_one_issuer_is_auto_muted(db):
    from app.models import Customer, Notification
    from app.services.contract_sync import apply_contract, detect_spam
    from app.services.notifier import enqueue

    spammer = 93333333
    for i in range(15):
        apply_contract(db, CORP, raw_contract(contract_id=5000 + i, issuer_id=spammer,
                                              date_issued=_recent()))
    enqueue(db, ref="contract:5000", kind="new_contract", title="x", subject_id=spammer)
    db.flush()
    flagged = detect_spam(db, {spammer})
    db.commit()

    assert [c.character_id for c in flagged] == [spammer]
    cust = db.get(Customer, spammer)
    assert cust.muted and "15 contracts" in cust.flag_reason
    statuses = {n.kind: n.status for n in db.scalars(select(Notification))}
    assert statuses["new_contract"] == "dropped"          # their queued ping was cancelled
    assert statuses["spam_detected"] == "pending"         # you get exactly one warning
    assert detect_spam(db, {spammer}) == []               # already muted: no repeat warning


def test_backlog_of_old_contracts_is_not_spam(db):
    """First sync imports 30 days of history; a busy legit customer must not trip it."""
    from app.services.contract_sync import apply_contract, detect_spam
    for i in range(30):
        apply_contract(db, CORP, raw_contract(contract_id=6000 + i,
                                              date_issued=_recent(minutes_ago=60 * 24 * (i + 1))))
    db.flush()
    assert detect_spam(db, {90000001}) == []


def test_silenced_issuer_gets_no_alerts(db):
    from app.models import Customer, Notification
    from app.services.contract_sync import _queue_alerts, apply_contract

    r = apply_contract(db, CORP, raw_contract(contract_id=7000))
    db.flush()
    db.get(Customer, 90000001).blocked = True
    db.flush()
    db.refresh(r.contract)
    _queue_alerts(db, r)
    assert db.scalars(select(Notification)).all() == []


# ---------------------------------------------------------------- ESI frugality

def test_failed_name_batch_isolates_bad_ids_with_few_errors():
    from app.esi import client as esi
    from app.esi.resolver import isolate_failures

    calls = {"n": 0, "errors": 0}
    bad = {7, 42}

    async def lookup(batch):
        calls["n"] += 1
        if bad & set(batch):
            calls["errors"] += 1
            raise esi.EsiError(404, "bad id")
        return [{"id": i, "name": f"n{i}", "category": "character"} for i in batch]

    rows, failed = asyncio.run(isolate_failures(list(range(64)), lookup))
    assert sorted(failed) == [7, 42] and len(rows) == 62
    # 1 (whole batch) + 2 (failing chunks) + 2 (bad ids) — near the minimum possible
    assert calls["errors"] == 5


def test_lookup_budget_caps_calls():
    from app.esi.resolver import LookupBudget
    b = LookupBudget(2)
    assert [b.take() for _ in range(4)] == [True, True, False, False]


def test_rate_limit_low_threshold():
    from app.esi.client import rate_limit_low
    assert rate_limit_low("9", "150/15m")       # under min 10
    assert not rate_limit_low("100", "150/15m")
    assert rate_limit_low("90", "1000/15m")     # under 10%
    assert not rate_limit_low(None, None)
