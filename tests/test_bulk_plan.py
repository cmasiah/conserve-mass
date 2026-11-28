"""Multi-pickup trip planning for bulk jobs."""
import pytest

from app.services.bulk_plan import Cargo, Hop, PickupCargo, plan, steps

# Systems on a line, 5 LY apart per step (one jump each): base=0, dest=1, pickups 2..5.
POS = {0: 0, 1: 4, 2: 3, 3: 3.5, 4: 8, 5: 12}
NAMES = {0: "AC2E-3", 1: "C-N4OD", 2: "LBGI-2", 3: "WMH-SO", 4: "LJ-TZW", 5: "006-L3"}


def route(a, b):
    d = abs(POS[a] - POS[b])
    if d == 0:
        return Hop(0, [], [])
    n = max(1, int(d // 5 + (d % 5 > 0)))
    return Hop(n, [d * 5 / n] * n, [f"mid{a}{b}{i}" for i in range(n - 1)])


def hauler(name, cap, cid=1):
    from app.services.jump import BONUS_DEFAULTS, Hauler, Profile
    p = Profile(cid, name, ship_name="Nomad", hull_cargo=1, hull_fuel=1000, range_ly=10,
                fuel_per_ly=1000, ore=3, bonus=dict(BONUS_DEFAULTS))
    p.fuel_type = 16274
    return Hauler(p, capacity_override=cap)


def pick(sid, *items):
    return PickupCargo(sid, NAMES[sid], 5000 + sid, NAMES[sid] + " - Athanor",
                       [Cargo(tid, n, q, u, 10.0) for tid, n, q, u in items])


def test_full_loads_then_small_pickups_share_a_trip():
    pickups = [pick(2, (34, "Tritanium", 50_000_000, 0.01)),      # 500k m3: 2 trips
               pick(3, (35, "Pyerite", 2_000_000, 0.01)),          # 20k: tops up
               pick(4, (36, "Mexallon", 3_000_000, 0.01))]         # 30k
    jfs = [hauler("A", 343_000, 1), hauler("B", 330_000, 2)]
    out = plan(pickups, jfs, (0, "AC2E-3"), (1, "C-N4OD"), route)
    trips = out["trips"]
    assert sum(t["m3"] for t in trips) == pytest.approx(550_000)
    assert all(t["m3"] <= t["capacity"] + 1 for t in trips)
    assert len(trips) == 2 and out["totals"]["waves"] == 1
    # The smaller pickups ride along in the second JF's spare room, not trips of their own.
    systems = [{s["system"] for s in t["stops"]} for t in trips]
    assert {"WMH-SO", "LJ-TZW"} <= set().union(*systems)
    assert not any(st == {"WMH-SO"} or st == {"LJ-TZW"} for st in systems)


def test_legs_and_fits_out_empty_back_empty():
    out = plan([pick(4, (34, "Tritanium", 1_000_000, 0.01))], [hauler("A", 343_000)],
               (0, "AC2E-3"), (1, "C-N4OD"), route)
    t = out["trips"][0]
    assert [(lg["from"], lg["to"], lg["fit"]) for lg in t["legs"]] == [
        ("AC2E-3", "LJ-TZW", "empty"), ("LJ-TZW", "C-N4OD", "loaded"), ("C-N4OD", "AC2E-3", "empty")]
    # Empty legs burn less than the loaded one per LY (economizers vs expanders).
    e, l = t["legs"][0], t["legs"][1]
    assert e["fuel"] / e["ly"] < l["fuel"] / l["ly"]
    kinds = [s["kind"] for s in steps(t)]
    assert kinds == ["jump", "load", "jump", "deliver", "jump"]
    load = steps(t)[1]
    assert load["refit"] and load["stop"]["items"][0]["qty"] == 1_000_000


def test_destination_is_home_no_return_leg():
    out = plan([pick(2, (34, "Tritanium", 1_000_000, 0.01))], [hauler("A", 343_000)],
               (0, "AC2E-3"), (0, "AC2E-3"), route)
    t = out["trips"][0]
    assert [lg["to"] for lg in t["legs"]] == ["LBGI-2", "AC2E-3"]
    assert [s["kind"] for s in steps(t)] == ["jump", "load", "jump", "deliver"]
    assert not steps(t)[-1]["refit"]


def test_bulky_items_stay_whole_and_no_scraps():
    pickups = [pick(2, (21009, "Capital Armor Plates", 40, 10_000.0),
                    (34, "Tritanium", 10_000_000, 0.01))]
    out = plan(pickups, [hauler("A", 343_000), hauler("B", 343_000, 2)],
               (0, "AC2E-3"), (1, "C-N4OD"), route)
    for t in out["trips"]:
        for s in t["stops"]:
            for i in s["items"]:
                if i["name"] == "Tritanium" and i["qty"] < 10_000_000:
                    assert i["m3"] >= 1000          # never a tiny top-off
    plates = sum(i["qty"] for t in out["trips"] for s in t["stops"] for i in s["items"]
                 if i["name"] == "Capital Armor Plates")
    assert plates == 40


def test_unreachable_and_too_big_are_reported():
    def no_5(a, b):
        return None if 5 in (a, b) else route(a, b)
    out = plan([pick(5, (34, "Tritanium", 100, 0.01)), pick(2, (99, "Titan", 1, 10_000_000.0))],
               [hauler("A", 343_000)], (0, "AC2E-3"), (1, "C-N4OD"), no_5)
    assert out["unreachable"] == ["006-L3"] and out["too_big"] == ["Titan at LBGI-2"]
    assert out["trips"] == []


# ------------------------------------------------------------------ saved plans (DB)

import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402

from sqlalchemy import select  # noqa: E402

from tests.test_bulk import ISSUER, _map, _store, bulk_raw  # noqa: E402
from tests.test_characters import client  # noqa: E402,F401
from tests.test_contract_sync import CORP  # noqa: E402
from tests.test_db_locking import seeded  # noqa: E402,F401


def _job_with_items(db):
    from app.models import BulkJob, Contract, ContractItem
    from app.services import bulk
    _map(db)
    _store(db, [bulk_raw(1, 5001, 30, status="finished", volume=400_000.0),
                bulk_raw(2, 5002, 25, volume=20_000.0)])
    db.add_all([ContractItem(record_id=11, contract_id=1, type_id=34, quantity=40_000_000,
                             is_included=True, unit_value=4.0),
                ContractItem(record_id=21, contract_id=2, type_id=35, quantity=2_000_000,
                             is_included=True, unit_value=10.0)])
    bulk.assign_jobs(db, list(db.scalars(select(Contract))))
    for c in db.scalars(select(Contract)):
        bulk._value_contract(db, c, {34: 4.0, 35: 10.0})
    db.commit()
    return db.scalars(select(BulkJob)).one()


def _fakes(monkeypatch):
    from app.services import bulk_plan, jump

    async def vols(type_ids):
        return {34: 0.01, 35: 0.01}

    def profs(db, enabled_only=False):
        return [hauler("Hauler A", 343_000, 101).profile, hauler("Hauler B", 330_000, 102).profile]
    monkeypatch.setattr(bulk_plan, "fill_unit_volumes", vols)
    monkeypatch.setattr(jump, "profiles", profs)
    # LJ-TZW=11, Y-2ANO=12 in _map; hubs AC2E-3=1, C-N4OD=2.
    pos = {1: 0, 2: 10, 11: 12, 12: 1}
    monkeypatch.setattr(bulk_plan, "_route_fn", lambda db, p: (
        lambda a, b: Hop(0, [], []) if a == b else Hop(max(1, round(abs(pos[a] - pos[b]) / 5)),
                                                        [abs(pos[a] - pos[b])], [])))
    real = jump.Hauler

    class FixedHauler(real):
        @property
        def capacity(self):
            return 343_000 if self.profile.character_id == 101 else 330_000
    monkeypatch.setattr(jump, "Hauler", FixedHauler)


def test_plan_saves_ticks_and_replans_keeping_started_trips(client, seeded, monkeypatch):
    from app.models import BulkJob
    from app.services import bulk_plan
    job = _job_with_items(seeded)
    _fakes(monkeypatch)
    page = client.get(f"/contracts/bulk/{job.id}").text
    assert "Plan trips" in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.post(f"/contracts/bulk/{job.id}/plan", data={"csrf": csrf})
    seeded.expire_all()
    job = seeded.get(BulkJob, job.id)
    data = json.loads(job.plan.data)
    assert data["base"] == "AC2E-3" and data["dest"] == "C-N4OD"
    assert sum(t["m3"] for t in data["trips"]) == pytest.approx(420_000)
    first = data["trips"][0]
    assert first["legs"][0]["from"] == "AC2E-3" and first["legs"][0]["fit"] == "empty"

    page = client.get(f"/contracts/bulk/{job.id}").text
    assert "Wave 1" in page and "Load at" in page and "Copy load" in page and "Re-plan" in page
    # Tick the first trip's first jump in the background.
    step = bulk_plan.steps(first)[0]["id"]
    r = client.post(f"/contracts/bulk/{job.id}/step", data={"csrf": csrf, "step": step, "done": 1},
                    headers={"X-Requested-With": "fetch"})
    assert r.json() == {"ok": True, "done": 1}
    seeded.expire_all()
    assert seeded.get(BulkJob, job.id).status == "hauling"

    # Change the destination: the plan is out of date; re-planning keeps the started trip.
    job = seeded.get(BulkJob, job.id)
    job.dest_system_id, job.dest_override = 1, True
    seeded.commit()
    assert "The job changed since this plan" in client.get(f"/contracts/bulk/{job.id}").text
    client.post(f"/contracts/bulk/{job.id}/plan", data={"csrf": csrf})
    seeded.expire_all()
    data2 = json.loads(seeded.get(BulkJob, job.id).plan.data)
    assert data2["trips"][0]["id"] == first["id"] and data2["trips"][0].get("kept")
    assert step in json.loads(seeded.get(BulkJob, job.id).plan.done)
    # The kept trip's cargo isn't planned twice.
    assert sum(t["m3"] for t in data2["trips"]) == pytest.approx(420_000)
    assert "Still there" in client.get(f"/contracts/bulk/{job.id}").text


def test_costs_ladder_and_use_this(client, seeded, monkeypatch):
    from app.db import utcnow
    from app.models import BulkJob, MarketPrice
    job = _job_with_items(seeded)
    _fakes(monkeypatch)
    seeded.add(MarketPrice(type_id=16274, jita_buy=600.0, source="fuzzwork", updated_at=utcnow()))
    seeded.commit()

    async def no_prices(type_ids):
        return None
    from app.services import prices
    monkeypatch.setattr(prices, "ensure", no_prices)
    page = client.get(f"/contracts/bulk/{job.id}").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.post(f"/contracts/bulk/{job.id}/plan", data={"csrf": csrf})
    client.post(f"/contracts/bulk/{job.id}/pricing", data={"csrf": csrf, "isk_per_jump": "5m",
                                                           "risk_pct": "1"})
    from app.services import bulk_plan
    seeded.expire_all()
    job = seeded.get(BulkJob, job.id)
    c = bulk_plan.costs(seeded, job, json.loads(job.plan.data))
    fuel = sum(n for _name, n, _isk in c["fuel"])
    assert c["fuel_isk"] == pytest.approx(fuel * 600.0)
    assert c["time_isk"] == pytest.approx(5e6 * c["jumps"])
    assert c["risk_isk"] == pytest.approx(0.01 * job.value_isk)
    assert [r["pct"] for r in c["ladder"]] == [15, 25, 50, 100]
    assert c["ladder"][0]["reward"] == pytest.approx(c["total"] * 1.15)
    page = client.get(f"/contracts/bulk/{job.id}").text
    assert "Operating cost" in page and "+100%" in page and "Use this" in page
    pick = c["ladder"][1]["reward"]
    client.post(f"/contracts/bulk/{job.id}/pricing", data={"csrf": csrf, "use": str(round(pick))})
    seeded.expire_all()
    assert seeded.get(BulkJob, job.id).agreed_reward_isk == pytest.approx(round(pick))


def test_return_contract_is_spotted_and_closes_the_job(db):
    from app.models import BulkJob, Contract
    from app.services import bulk
    _store(db, [bulk_raw(1, 5001, 120, status="finished")])
    bulk.assign_jobs(db, list(db.scalars(select(Contract))))
    db.commit()
    job = db.scalars(select(BulkJob)).one()
    now = "2099-01-01T00:00:00Z"
    ret = {"contract_id": 777, "type": "item_exchange", "status": "outstanding",
           "issuer_id": 2112000001, "issuer_corporation_id": CORP, "for_corporation": True,
           "assignee_id": ISSUER, "price": 1.5e9, "date_issued": now}
    other = dict(ret, contract_id=778, assignee_id=12345)          # someone else
    assert bulk.match_returns(db, CORP, [ret, other]) == 1
    db.commit()
    job = db.get(BulkJob, job.id)
    assert (job.return_contract_id, job.return_status, job.paid_at) == (777, "outstanding", None)
    # The requester accepts it: delivered, paid, earned = the price.
    bulk.match_returns(db, CORP, [dict(ret, status="finished", date_completed=now)])
    db.commit()
    job = db.get(BulkJob, job.id)
    assert job.status == "delivered" and job.paid_at and job.earned_isk == 1.5e9
    from app.services.home import bulk_earnings
    from datetime import datetime, timezone
    assert bulk_earnings(db, datetime(2000, 1, 1, tzinfo=timezone.utc)) == [(job.paid_at, 1.5e9)]


def test_same_item_from_two_contracts_is_one_line():
    p = pick(2, (34, "Tritanium", 1_000_000, 0.01), (34, "Tritanium", 500_000, 0.012))
    out = plan([p], [hauler("A", 343_000)], (0, "AC2E-3"), (1, "C-N4OD"), route)
    items = out["trips"][0]["stops"][0]["items"]
    assert len(items) == 1 and items[0]["qty"] == 1_500_000
