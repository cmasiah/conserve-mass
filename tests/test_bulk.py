"""Bulk hauling jobs: item-exchange contracts assigned to the corp."""
import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from tests.test_characters import client  # noqa: F401 (fixture)
from tests.test_contract_sync import CORP, raw_contract
from tests.test_db_locking import seeded  # noqa: F401 (fixture)

ISSUER = 90000777
LY = 9_460_730_472_580_800


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def bulk_raw(cid, loc, minutes_ago=30, status="outstanding", issuer=ISSUER, **over):
    now = datetime.now(timezone.utc)
    fields = dict(contract_id=cid, type="item_exchange", status=status, issuer_id=issuer,
                  start_location_id=loc, end_location_id=loc, price=0.0, reward=0.0,
                  collateral=0.0, volume=50_000.0, days_to_complete=0, title="C-N",
                  date_issued=_iso(now - timedelta(minutes=minutes_ago)))
    fields.update(over)
    return raw_contract(**fields)


def _map(db):
    """Two hubs and two pickup systems: P1 is near C-N4OD, P2 near AC2E-3."""
    from app.models import Location, MapSystem
    for sid, name, x in ((1, "AC2E-3", 0), (2, "C-N4OD", 10), (11, "LJ-TZW", 12), (12, "Y-2ANO", 1)):
        db.add(MapSystem(system_id=sid, name=name, constellation_id=1, region_id=1,
                         region_name="Fountain", security=-0.2, x=x * LY, y=0.0, z=0.0))
    for loc, sid, name in ((5001, 11, "LJ-TZW - Keepstar"), (5002, 12, "Y-2ANO - Fortizar")):
        db.add(Location(location_id=loc, kind="structure", name=name, system_id=sid,
                        system_name=name.split(" ")[0]))
    db.commit()


def _store(db, rows):
    from app.services.contract_sync import store_batch
    out = store_batch(db, CORP, rows, {})
    db.commit()
    return out


def test_many_bulk_contracts_are_not_spam_and_send_no_courier_alerts(db):
    from app.models import Customer, Notification
    rows = [bulk_raw(2000 + i, 5001) for i in range(14)]
    _store(db, rows)
    cust = db.get(Customer, ISSUER)
    assert not cust.muted                                   # 14 at once is normal for bulk
    kinds = set(db.scalars(select(Notification.kind)))
    assert not kinds & {"new_contract", "spam_detected"}


def test_grouping_by_issuer_within_two_hours(db):
    from app.models import BulkJob, Contract
    from app.services import bulk
    _store(db, [bulk_raw(1, 5001, 10), bulk_raw(2, 5002, 90),        # same job (80 min apart)
                bulk_raw(3, 5001, 400),                              # 5h earlier: its own job
                bulk_raw(4, 5001, 20, issuer=90000888)])             # someone else
    bulk.assign_jobs(db, list(db.scalars(select(Contract))))
    db.commit()
    jobs = db.scalars(select(BulkJob)).all()
    sizes = sorted(len(j.contracts) for j in jobs)
    assert sizes == [1, 1, 2]
    two = next(j for j in jobs if len(j.contracts) == 2)
    assert {c.contract_id for c in two.contracts} == {1, 2} and two.price_mode == "after"


def test_after_sync_reads_items_values_them_picks_hub_and_notifies(db, monkeypatch):
    from app.esi import client as esi
    from app.models import BulkJob, Contract, ContractItem, Notification
    from app.services import bulk, prefs, prices
    _map(db)
    prefs.save(db, {"kind_bulk_job": "1"})
    db.commit()
    _store(db, [bulk_raw(1, 5001, 30, status="finished"), bulk_raw(2, 5001, 20),
                bulk_raw(3, 5002, 25)])

    async def get(path, token=None, params=None):
        cid = int(path.rstrip("/").split("/")[-2])
        return [{"record_id": cid * 10 + 1, "type_id": 34, "quantity": 1_000_000, "is_included": True},
                {"record_id": cid * 10 + 2, "type_id": 23919, "quantity": 1, "raw_quantity": -2,
                 "is_included": True}]

    async def fuzz(type_ids):
        return {34: (4.0, 4.5), 23919: (2e9, 2.1e9)}

    async def names(db, ids):
        return {}
    monkeypatch.setattr(esi, "get", get)
    monkeypatch.setattr(prices, "_fuzzwork", fuzz)
    monkeypatch.setattr(bulk.resolver, "resolve_names", names)
    asyncio.run(bulk.after_sync(CORP, "tok"))

    db.expire_all()
    assert db.query(ContractItem).count() == 6
    c1 = db.get(Contract, 1)
    assert c1.value_isk == 4_000_000                     # the blueprint copy counts as 0
    job = db.scalars(select(BulkJob)).one()
    assert job.status == "accepted"                      # contract 1 is in our hands
    # 100k m3 picked up at LJ-TZW (2 LY from C-N4OD) vs 50k at Y-2ANO (1 LY from AC2E-3).
    assert job.dest_system_id == 2
    assert job.exposure_isk == 4_000_000                 # only the accepted contract
    note = db.scalars(select(Notification).where(Notification.kind == "bulk_job")).one()
    assert "3 contracts" in note.line and "price after" in note.line
    # Its goods aren't ours: Materials must not count them.
    assert bulk.reserved_quantities(db) == {34: 1_000_000, 23919: 1}


def test_materials_leave_out_goods_from_open_bulk_jobs(db):
    from app.models import Asset, Contract, ContractItem, TypeInfo
    from app.services import bulk
    from app.services.assets import material_lines
    _store(db, [bulk_raw(1, 5001, 30, status="finished")])
    db.add(ContractItem(record_id=1, contract_id=1, type_id=34, quantity=600, is_included=True))
    db.add(TypeInfo(type_id=34, name="Tritanium", group_id=18, category_id=4))
    db.add(Asset(item_id=1, owner_type="corporation", owner_id=CORP, type_id=34, quantity=1000,
                 location_id=5001, location_flag="CorpSAG1", site_id=5001))
    bulk.assign_jobs(db, list(db.scalars(select(Contract))))
    db.commit()
    _sections, every = material_lines(db, "all")
    trit = next(ln for ln in every if ln.type_id == 34)
    assert trit.quantity == 400                     # 1,000 held - 600 someone else's


def test_bulk_pages_and_editing(client, seeded):
    from app.models import BulkJob, Contract
    from app.services import bulk
    _map(seeded)
    _store(seeded, [bulk_raw(1, 5001, 30), bulk_raw(2, 5002, 20, reward=500_000_000.0)])
    bulk.assign_jobs(seeded, list(seeded.scalars(select(Contract))))
    seeded.commit()
    job = seeded.scalars(select(BulkJob)).one()

    html = client.get("/contracts").text
    assert "Hauling Contracts" in html and ">Couriers</a>" in html and "Bulk jobs" in html
    assert "#1</a>" not in html                     # couriers tab doesn't list bulk contracts
    lst = client.get("/contracts/bulk").text
    assert f"/contracts/bulk/{job.id}" in lst and "💰" in lst   # one contract pays ISK
    page = client.get(f"/contracts/bulk/{job.id}").text
    assert "LJ-TZW" in page and "Y-2ANO" in page and "→ C-N4OD" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = client.post(f"/contracts/bulk/{job.id}", data={
        "csrf": csrf, "status": "delivered", "dest": "1", "agreed": "", "collateral": "40b",
        "title": "Small Industries Nov", "notes": ""}, follow_redirects=False)
    assert r.status_code == 303
    seeded.expire_all()
    job = seeded.get(BulkJob, job.id)
    assert (job.status, job.dest_system_id, job.dest_override) == ("delivered", 1, True)
    assert job.return_collateral_isk == 40e9 and job.delivered_at and job.exposure_isk == 0


def test_isk_text():
    from app.web.routes.bulk import _isk
    assert _isk("1.8b") == 1.8e9 and _isk("250m") == 250e6 and _isk("12,500,000") == 12.5e6
    assert _isk("") is None and _isk("abc") is None


def test_fuzzwork_parsing():
    from app.services.prices import parse_fuzzwork
    data = {"34": {"buy": {"percentile": "4.10", "max": "4.20"}, "sell": {"percentile": "4.5"}},
            "35": {"buy": {"percentile": "0", "max": "9.9"}, "sell": {}}, "x": {}}
    assert parse_fuzzwork(data) == {34: (4.1, 4.5), 35: (9.9, None)}


def test_exposure_and_hauled_across_couriers_and_bulk(client, seeded):
    from app.models import BulkJob, Contract
    from app.services import bulk, exposure
    from app.db import utcnow
    now = datetime.now(timezone.utc)
    # A courier in transit (3B collateral) and one delivered (2B).
    _store(seeded, [raw_contract(contract_id=10, status="in_progress", collateral=3e9,
                                 date_accepted=_iso(now - timedelta(hours=5))),
                    raw_contract(contract_id=11, status="finished", collateral=2e9,
                                 date_accepted=_iso(now - timedelta(days=2)),
                                 date_completed=_iso(now - timedelta(days=1)))])
    # A bulk job: one contract accepted (goods worth 40B in our hands), one still outstanding.
    _store(seeded, [bulk_raw(1, 5001, 30, status="finished",
                             date_completed=_iso(now - timedelta(minutes=20))),
                    bulk_raw(2, 5001, 25)])
    bulk.assign_jobs(seeded, list(seeded.scalars(select(Contract).where(
        Contract.type == "item_exchange"))))
    seeded.get(Contract, 1).value_isk = 40e9
    seeded.get(Contract, 2).value_isk = 5e9
    seeded.commit()
    e = exposure.summary(seeded, utcnow())
    assert e.now == 43e9 and e.couriers == 1 and e.jobs == 1
    assert e.hauled_all == 2e9
    assert e.daily[-1]["isk"] == 43e9
    per = exposure.per_customer(seeded)[ISSUER]
    assert per["exposure"] == 40e9 and per["bulk_jobs"] == 1

    # Deliver the job with a 45B return collateral: exposure drops, hauled grows by it.
    job = seeded.scalars(select(BulkJob)).one()
    job.status, job.delivered_at, job.return_collateral_isk = "delivered", utcnow(), 45e9
    seeded.commit()
    e = exposure.summary(seeded, utcnow())
    assert e.now == 3e9 and e.hauled_all == 47e9

    dash = client.get("/hauling").text
    assert "Exposure now" in dash and "Collateral hauled" in dash and "exp-bar" in dash
    cust = client.get(f"/customers/{ISSUER}").text
    assert "Bulk jobs" in cust and "Collateral hauled" in cust
    lst = client.get("/customers").text            # bulk-only customer is listed
    assert f"/customers/{ISSUER}" in lst and "In hand now" in lst
