from datetime import datetime, timedelta, timezone

from sqlalchemy import select

CORP = 98000001


def raw_contract(**over):
    now = datetime.now(timezone.utc)
    base = {
        "contract_id": 1001, "type": "courier", "status": "outstanding",
        "issuer_id": 90000001, "issuer_corporation_id": 98000099, "assignee_id": CORP,
        "acceptor_id": 0, "start_location_id": 60003760, "end_location_id": 1022734985679,
        "volume": 320000.0, "reward": 250_000_000.0, "collateral": 3_000_000_000.0,
        "days_to_complete": 3, "for_corporation": False, "availability": "personal",
        "date_issued": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "date_expired": (now + timedelta(days=6)).isoformat().replace("+00:00", "Z"),
        "title": "pls no gank",
    }
    base.update(over)
    return base


def test_ignores_non_courier_and_other_assignees(db):
    from app.services.contract_sync import apply_contract
    assert apply_contract(db, CORP, raw_contract(type="item_exchange")) is None
    assert apply_contract(db, CORP, raw_contract(assignee_id=123)) is None


def test_new_contract_creates_customer_and_event(db):
    from app.models import Contract, ContractEvent, Customer
    from app.services.contract_sync import apply_contract

    r = apply_contract(db, CORP, raw_contract())
    db.commit()
    assert r.is_new and r.changes == []
    c = db.get(Contract, 1001)
    assert c.status == "outstanding" and c.work_status == "new"
    assert db.get(Customer, 90000001) is not None
    assert len(db.scalars(select(ContractEvent)).all()) == 1


def test_status_change_records_event_and_updates_work_status(db):
    from app.models import Contract
    from app.services.contract_sync import apply_contract

    apply_contract(db, CORP, raw_contract())
    db.commit()
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    r = apply_contract(db, CORP, raw_contract(status="in_progress", acceptor_id=95000001,
                                              date_accepted=now))
    db.commit()
    fields = {f for f, _, _ in r.changes}
    assert {"status", "acceptor_id", "date_accepted"} <= fields
    c = db.get(Contract, 1001)
    assert c.work_status == "accepted"
    assert c.deadline is not None


def test_notifications_are_deduplicated(db):
    from app.models import Notification
    from app.services.notifier import enqueue

    assert enqueue(db, ref="contract:1", kind="new_contract", title="x")
    assert not enqueue(db, ref="contract:1", kind="new_contract", title="x")
    db.commit()
    assert len(db.scalars(select(Notification)).all()) == 1


def test_customer_stats(db):
    from app.services.contract_sync import apply_contract
    from app.services.customers import customer_stats

    apply_contract(db, CORP, raw_contract(contract_id=1, status="finished"))
    apply_contract(db, CORP, raw_contract(contract_id=2, status="outstanding"))
    db.commit()
    [s] = customer_stats(db)
    assert s.total == 2 and s.completed == 1 and s.isk_earned == 250_000_000.0
