"""Jump planner: ship math, routing rules, courier lanes, map import, pages."""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from tests.test_blueprints_esi import NOW, fake  # noqa: F401 (fixture)

LY = 9_460_730_472_580_800
REGION = 10000058


def _sys(db, sid, name, x_ly, sec=-0.3, y_ly=0.0, region=REGION):
    from app.models import MapSystem
    db.add(MapSystem(system_id=sid, name=name, constellation_id=1, region_id=region,
                     region_name="Fountain" if region == REGION else "Other", security=sec,
                     x=x_ly * LY, y=y_ly * LY, z=0.0))


@pytest.fixture()
def world(db):
    """A line of systems, distances in LY:
       Jita(hs) 0 ─ Low1 4.5 ─ Low2 9 ─ Null3 13.5 ─ AC2E 18 ;  Detour 9.5 (2 LY off-axis)
       HsEnd(hs) 22.5 near Lowend 21.5 ; Poch 4 (Pochven) ; HsMid 6 (high-sec)."""
    _sys(db, 30000142, "Jita", 0, sec=0.95)
    _sys(db, 1, "Low1", 4.5, sec=0.3)
    _sys(db, 2, "Low2", 9, sec=0.2)
    _sys(db, 3, "Null3", 13.5)
    _sys(db, 4, "AC2E-3", 18, sec=-0.4)
    _sys(db, 5, "Detour", 9.5, y_ly=2)
    _sys(db, 6, "HsEnd", 22.5, sec=0.7)
    _sys(db, 7, "Lowend", 21.5, sec=0.4)
    _sys(db, 8, "Poch", 4, region=10000070)
    _sys(db, 9, "HsMid", 6, sec=0.6)
    db.commit()
    from app.services import jump
    jump._GRAPHS.clear()
    return db


def _profile(range_ly=10.0, fuel=1000.0, cargo=350_000.0, collateral=None):
    from app.services.jump import Profile
    return Profile(1, "Pilot", ship_name="Nomad", range_ly=range_ly, fuel_per_ly=fuel,
                   cargo_m3=cargo, fuel_type=17889, max_collateral=collateral)


# ------------------------------------------------------------------ ship math

def test_profile_applies_skills_fit_and_overrides(db):
    from app.models import Asset, Character, CharacterSkill, EntityName, JumpPilot
    from app.services import jump
    db.add(Character(character_id=1, name="Hauler"))
    for sid, name, lvl in [(21611, "Jump Drive Calibration", 5), (21610, "Jump Fuel Conservation", 5),
                           (29029, "Jump Freighters", 4), (20528, "Minmatar Freighter", 5),
                           (28846, "Nomad", 0), (5001, "Prototype Jump Drive Economizer", 0),
                           (5002, "ORE Expanded Cargohold", 0), (5003, "Expanded Cargohold II", 0)]:
        db.add(EntityName(entity_id=sid, name=name, category="inventory_type"))
        if lvl:
            db.add(CharacterSkill(character_id=1, skill_id=sid, trained_level=lvl, active_level=lvl))
    ship = 9_000_000_001
    db.add(Asset(item_id=ship, owner_type="character", owner_id=1, type_id=28846, quantity=1,
                 location_id=60003760, site_id=60003760, updated_at=NOW))
    for i, (t, flag) in enumerate([(5001, "LoSlot0"), (5002, "LoSlot1"), (5003, "LoSlot2")]):
        db.add(Asset(item_id=ship + 1 + i, owner_type="character", owner_id=1, type_id=t,
                     quantity=1, location_id=ship, location_flag=flag, site_id=60003760,
                     container_id=ship, updated_at=NOW))
    jump._set_json(db, "jump:ship:28846", {"name": "Nomad", "range": 5.0, "fuel": 10000,
                                           "fuel_type": 17889, "cargo": 100000})
    jp = JumpPilot(character_id=1)
    db.add(jp)
    db.commit()
    p = jump.profile(db, jp, db.get(Character, 1), jump.bonuses(db))
    # ORE and T2 expanders are told apart.
    assert (p.ship_name, p.economizers, p.ore, p.t2, p.fit_source) == ("Nomad", 1, 1, 1, "assets")
    assert p.range_ly == pytest.approx(10.0)                       # 5 × (1 + 0.2×5)
    assert p.fuel_per_ly == pytest.approx(10000 * 0.5 * 0.6 * 0.9)  # JFC5, JF4, 1 economizer
    assert p.cargo_m3 == pytest.approx(100000 * 1.25 * 1.29 * 1.275)  # racial 5, ORE + T2
    assert p.fuel_name == "Hydrogen Isotopes"
    jp.fuel_override, jp.economizers, jp.expanders, jp.t2_expanders = 2500, 3, 0, 0
    p = jump.profile(db, jp, db.get(Character, 1), jump.bonuses(db))
    assert p.fuel_per_ly == 2500 and "fuel_per_ly" in p.overridden and p.fit_source == "manual"
    assert p.cargo_m3 == pytest.approx(100000 * 1.25)
    # Leg-specific numbers for the packing ledger: loaded with 3 ORE, empty with 3 economizers.
    assert p.cargo_with(3) == pytest.approx(100000 * 1.25 * 1.29 ** 3)
    assert p.fuel_with(3) == pytest.approx(10000 * 0.5 * 0.6 * 0.9 ** 3)


def test_nomad_with_three_ore_expanders_matches_the_game():
    """In game: Nomad, Minmatar Freighter V, 3x ORE Expanded Cargohold = 354,203.7 m3;
    three economizers carried for the trip back take 3 x 3,500 m3 of it."""
    from app.services.jump import BONUS_DEFAULTS, Profile
    p = Profile(1, "JF", ship_name="Nomad", hull_cargo=132_000 * 1.25, hull_fuel=1000,
                range_ly=10, fuel_per_ly=1000, ore=3, bonus=dict(BONUS_DEFAULTS))
    p.cargo_m3 = p.cargo_with(3)
    assert p.cargo_m3 == pytest.approx(354_203.7, abs=0.1)
    assert p.carried_econ == 3 and p.hold_m3 == pytest.approx(343_703.7, abs=0.1)
    assert p.empty_fuel_per_ly == pytest.approx(1000 * 0.9 ** 3)
    assert p.for_leg("empty").fuel_per_ly == pytest.approx(729) and p.for_leg("loaded") is p


def test_old_wrong_defaults_are_replaced_unless_from_game_data(db):
    from app.services import jump
    jump.save_bonuses(db, {"ore_expander_cargo": 0.275, "economizer_volume": 10500,
                           "economizer_fuel": 0.10})
    b = jump.bonuses(db)
    assert b["ore_expander_cargo"] == 0.29 and b["economizer_volume"] == 3500
    jump._set_json(db, "jump:module_source", {"economizer_volume": "10,500 m³"})
    assert jump.bonuses(db)["economizer_volume"] == 10500     # game data wins


# ------------------------------------------------------------------ routing

def test_fewest_jumps_then_least_distance(world):
    from app.services.jump import plan_route
    r = plan_route(world, 30000142, 4, _profile(range_ly=10))
    assert r.error is None
    # 18 LY with 10 LY range: 2 jumps minimum, via Low2 (9 + 9) rather than Detour (longer).
    assert [leg.to.name for leg in r.legs] == ["Low2", "AC2E-3"]
    assert r.total_ly == pytest.approx(18.0)
    assert r.fuel == 9000 + 9000


def test_short_range_never_lands_in_highsec_or_pochven(world):
    from app.services.jump import plan_route
    r = plan_route(world, 30000142, 4, _profile(range_ly=5))
    names = [leg.to.name for leg in r.legs]
    assert names == ["Low1", "Low2", "Null3", "AC2E-3"]
    assert "HsMid" not in names and "Poch" not in names
    assert all(leg.ly <= 5 for leg in r.legs)


def test_out_of_range_says_so(world):
    from app.services.jump import plan_route
    r = plan_route(world, 30000142, 4, _profile(range_ly=4))
    assert r.error and "No route within 4.00 LY" in r.error


def test_highsec_destination_lands_nearby_and_gates_in(world):
    from app.services.jump import plan_route
    r = plan_route(world, 4, 6, _profile(range_ly=10), gates={7: 2, 3: 9})
    assert r.landing.name == "Lowend" and r.gate_jumps == 2
    assert [leg.to.name for leg in r.legs] == ["Lowend"]


def test_only_via_cyno_points(world):
    from app.models import CynoPoint
    from app.services import jump
    world.add(CynoPoint(system_id=5, label="beacon"))   # Detour
    world.add(CynoPoint(system_id=4, label="home"))
    world.commit()
    jump._GRAPHS.clear()
    r = jump.plan_route(world, 30000142, 4, _profile(range_ly=10), only_cynos=True)
    assert [leg.to.name for leg in r.legs] == ["Detour", "AC2E-3"]


# ------------------------------------------------------------------ courier lanes

def _contract(db, cid, start, end, vol, reward, coll, status="outstanding"):
    from app.models import Contract, Customer
    if db.get(Customer, 90000001) is None:
        db.add(Customer(character_id=90000001, name="Customer"))
    db.add(Contract(contract_id=cid, corporation_id=98000001, issuer_id=90000001, type="courier",
                    status=status, start_location_id=start, end_location_id=end, volume_m3=vol,
                    reward_isk=reward, collateral_isk=coll, date_issued=NOW - timedelta(hours=1)))


def test_pack_respects_cargo_and_collateral():
    from app.models import Contract
    from app.services.jump import pack
    cs = [Contract(contract_id=i, volume_m3=v, reward_isk=r, collateral_isk=c)
          for i, (v, r, c) in enumerate([(200_000, 100e6, 1e9), (200_000, 90e6, 1e9),
                                          (100_000, 80e6, 5e9), (400_000, 50e6, 1e9)])]
    trips, unfit = pack(cs, cargo=350_000, max_collateral=6e9)
    assert [c.contract_id for c in unfit] == [3]                 # bigger than the hold
    assert [sorted(c.contract_id for c in t.contracts) for t in trips] == [[0, 2], [1]]
    assert all(t.volume <= 350_000 and t.collateral <= 6e9 for t in trips)


def test_lanes_group_route_and_rank_by_isk_per_jump(world):
    from app.models import Location
    from app.services.jump import lanes
    for lid, sid in [(60003760, 30000142), (1040000000001, 4), (1040000000002, 3)]:
        world.add(Location(location_id=lid, name=f"Station {sid}", system_id=sid, kind="structure"))
    _contract(world, 1, 60003760, 1040000000001, 300_000, 200e6, 2e9)
    _contract(world, 2, 60003760, 1040000000001, 50_000, 60e6, 1e9)
    _contract(world, 3, 60003760, 1040000000002, 100_000, 150e6, 1e9, status="in_progress")
    world.commit()
    out = lanes(world, _profile(range_ly=10, cargo=320_000))
    # Jita→AC2E-3's best trip pays 200M over 2 jumps; Jita→Null3 pays 150M over 2.
    assert [(ln.start.name, ln.end.name) for ln in out] == [("Jita", "AC2E-3"), ("Jita", "Null3")]
    jita_ac = out[0]
    assert jita_ac.per_jump == pytest.approx(200e6 / 2)
    # 300k + 50k m³ doesn't fit a 320k hold: two trips, the bigger payer first.
    assert jita_ac.route.jumps == 2 and len(jita_ac.trips) == 2
    assert jita_ac.trips[0].reward == 200e6
    assert out[1].per_jump == pytest.approx(150e6 / 2)


# ------------------------------------------------------------------ map import + pages

def test_map_import_from_esi(db, fake):
    from app.models import MapSystem
    from app.services.jump import import_map, map_status
    fake({("GET", "/universe/systems"): [30000142, 30002001, 31000005],
          ("GET", "/universe/systems/30000142"): {"system_id": 30000142, "name": "Jita",
                                                 "constellation_id": 20000020, "security_status": 0.946,
                                                 "position": {"x": 1.0, "y": 2.0, "z": 3.0}},
          ("GET", "/universe/systems/30002001"): {"system_id": 30002001, "name": "AC2E-3",
                                                 "constellation_id": 20000300, "security_status": -0.38,
                                                 "position": {"x": 4.0, "y": 5.0, "z": 6.0}},
          ("GET", "/universe/constellations/20000020"): {"region_id": 10000002},
          ("GET", "/universe/constellations/20000300"): {"region_id": 10000058},
          ("GET", "/universe/regions/10000002"): {"name": "The Forge"},
          ("GET", "/universe/regions/10000058"): {"name": "Fountain"}})
    assert asyncio.run(import_map()) == 2                       # the wormhole is skipped
    db.expire_all()
    assert db.get(MapSystem, 30002001).region_name == "Fountain"
    assert map_status(db)["state"] == "done" and map_status(db)["systems"] == 2


@pytest.fixture()
def client(world):
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from app.main import app
    from app.models import Character, JumpPilot
    from app.services import jump
    world.add(Character(character_id=1, name="Hauler"))
    world.add(JumpPilot(character_id=1, ship_type_id=28846, range_override=10,
                        fuel_override=1000, cargo_override=350000))
    jump._set_json(world, "jump:ship:28846", {"name": "Nomad", "range": 5.0, "fuel": 10000,
                                              "fuel_type": 17889, "cargo": 100000})
    jump._set_json(world, "jump:isotope_prices", {"at": 9e12, "prices": {"17889": 500.0}})
    world.commit()

    async def login(request: Request):
        request.session["user"] = {"character_id": 1, "name": "Hauler"}
        request.session["csrf"] = "t"
        return {}
    app.add_api_route("/__test/login7", login)
    c = TestClient(app)
    c.get("/__test/login7")
    yield c
    app.router.routes[:] = [r for r in app.router.routes
                            if getattr(r, "path", "") != "/__test/login7"]


def test_route_page_shows_legs_fuel_and_cost(client):
    html = client.get("/jump?tab=route&frm=jita&to=ac2e-3").text
    assert "<strong>Low2</strong>" in html and "<strong>AC2E-3</strong>" in html
    assert "18,000" in html and "Hydrogen Isotopes" in html
    assert 'data-ac' in html                              # autocomplete on From / To
    hits = client.get("/jump/systems?q=ac2e").json()
    assert hits[0]["name"] == "AC2E-3" and hits[0]["region"] == "Fountain"


def test_trip_modes_plan_each_leg_with_its_own_fit(client, monkeypatch):
    import json
    import re

    from app.services import jump as svc

    async def paths(origins, dest):          # ESI /route: 2 gates from each candidate
        return {o: [o, 99, dest] for o in origins}
    monkeypatch.setattr(svc, "gate_paths", paths)
    fuel_of = lambda html: [int(v.replace(",", "")) for v in re.findall(
        r'kpi-v">([\d,]+)</span>\s*<span class="kpi-l">(?:📦 loaded|⛽ empty)', html)]
    loaded = client.get("/jump?tab=route&frm=jita&to=ac2e-3&trip=loaded").text
    empty = client.get("/jump?tab=route&frm=jita&to=ac2e-3&trip=empty").text
    rt = client.get("/jump?tab=route&frm=jita&to=ac2e-3&trip=empty_loaded").text
    (fl,), (fe,) = fuel_of(loaded), fuel_of(empty)
    assert fe < fl
    out, back = fuel_of(rt)                        # empty out, loaded back
    assert out == fe and back > 0 and "round trip" in rt
    assert "AC2E-3 → Jita" in rt                 # the return leg is planned on its own
    # The map's data and the steps are embedded for the browser.
    data = json.loads(re.search(r'id="jmap-data" type="application/json">(.*?)</script>',
                                rt, re.S).group(1))
    assert len(data["legs"]) == 2 and data["legs"][0]["fit"] == "empty"
    steps = json.loads(re.search(r'id="jmap-steps" type="application/json">(.*?)</script>',
                                 rt, re.S).group(1))
    assert steps[0]["kind"] == "jump" and steps[0]["role"].startswith("mid")
    assert any(st["role"] == "destination" for st in steps)
    assert "Step by step" in rt and "Jump to" in rt
    assert any(st["kind"] == "gates" and st["gates"] == 2 for st in steps)   # into Jita


def test_old_leg_parameter_still_works(client):
    assert "⛽ empty" in client.get("/jump?tab=route&frm=jita&to=ac2e-3&leg=empty").text


def test_map_data_north_up_gates_and_borders(world):
    from app.models import MapGate, MapSystem
    from app.services.jump import Leg, Route, map_data
    a = MapSystem(system_id=91, name="North", constellation_id=1, region_id=7, security=-0.5,
                  x=0.0, y=0.0, z=9.46e16)
    b = MapSystem(system_id=92, name="South", constellation_id=2, region_id=7, security=-0.5,
                  x=9.46e16, y=0.0, z=-9.46e16)
    world.add_all([a, b, MapGate(from_id=91, to_id=92), MapGate(from_id=92, to_id=91)])
    world.commit()
    m = map_data(world, [("North → South", "loaded", Route(a, b, legs=[Leg(a, b, 20.0, 100)]))], 10)
    nodes = {n["n"]: n for n in m["nodes"]}
    assert nodes["North"]["y"] < nodes["South"]["y"] and nodes["North"]["x"] < nodes["South"]["x"]
    assert m["gates"] == [[m["nodes"].index(nodes["North"]), m["nodes"].index(nodes["South"]), 1]]
    assert m["legs"][0]["jumps"][0][2] == 20.0 and m["has_gates"]


def test_gates_csv_is_read_both_ways():
    from app.services.jump import parse_gates_csv
    text = ("fromRegionID,fromConstellationID,fromSolarSystemID,toSolarSystemID,"
            "toConstellationID,toRegionID\n1,2,30000001,30000002,2,1\nbad,row\n")
    assert parse_gates_csv(text) == {(30000001, 30000002), (30000002, 30000001)}


def test_pilots_and_cyno_pages(client, world):
    from app.models import CynoPoint
    html = client.get("/jump?tab=pilots").text
    assert "Hauler" in html and "Bonus values" in html and 'name="range_override"' in html
    r = client.post("/jump/cynos/add", data={"csrf": "t", "system": "HsMid"}, follow_redirects=False)
    assert r.status_code == 303 and world.scalar(select(CynoPoint)) is None   # high-sec refused
    client.post("/jump/cynos/add", data={"csrf": "t", "system": "null3", "label": "alt"})
    world.expire_all()
    assert world.get(CynoPoint, 3).label == "alt"
    assert "Null3" in client.get("/jump?tab=cynos").text


# ------------------------------------------------------------------ search, feedback, ledger

def test_fuzzy_system_search(world):
    from app.services.jump import search_systems
    names = lambda q: [h["name"] for h in search_systems(world, q)]  # noqa: E731
    assert names("ac2e")[0] == "AC2E-3"          # dash ignored
    assert names("AC2E-3")[0] == "AC2E-3"
    assert names("jta") == ["Jita"]               # letters in order
    assert names("low")[:2] == ["Low1", "Low2"]  # starts-with before contains
    assert search_systems(world, "hsmid")[0]["cyno"] is False


def test_plan_route_without_map_or_pilot_explains_why(db):
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from app.main import app

    async def login(request: Request):
        request.session["user"] = {"character_id": 1, "name": "x"}
        return {}
    app.add_api_route("/__test/login8", login)
    try:
        c = TestClient(app)
        c.get("/__test/login8")
        html = c.get("/jump?tab=route&frm=jita&to=ac2e").text
        assert "Download the star map first" in html
        assert "Needs the star map" in html and "disabled" in html.split("Plan route")[0][-200:]
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/__test/login8"]


def test_parse_items_inventory_paste_and_plain_lines():
    from app.services.jump import parse_items
    paste = ("Tritanium\t25,000,000\tMineral\t\t\t250,000 m3\n"
             "Capital Armor Plates\t40\tCapital Construction Components\t\t\t400,000.00 m3\n"
             "Tritanium\t5,000,000\tMineral\t\t\t50,000 m3\n"
             "Nomad\t\tJump Freighter\t\t\t1,300,000 m3\n"
             "Megacyte 1,000\n"
             "Fullerides x 2500\n"
             "this line is junk\n")
    items, bad = parse_items(paste)
    by = {i.name: i for i in items}
    assert by["Tritanium"].qty == 30_000_000 and by["Tritanium"].unit_m3 == pytest.approx(0.01)
    assert by["Capital Armor Plates"].unit_m3 == pytest.approx(10_000)
    assert by["Nomad"].qty == 1 and by["Nomad"].unit_m3 == 1_300_000
    assert by["Megacyte"].qty == 1000 and by["Megacyte"].unit_m3 is None   # looked up later
    assert by["Fullerides"].qty == 2500
    assert bad == ["this line is junk"]


def _hauler(cid, cargo, fuel=2700, econ_vol=10_500):
    from app.services.jump import BONUS_DEFAULTS, Hauler, Profile
    p = Profile(cid, f"JF{cid}", ship_name="Nomad", range_ly=10, fuel_per_ly=fuel,
                cargo_m3=cargo, fuel_type=17889, hull_fuel=fuel, hull_cargo=cargo,
                bonus={**BONUS_DEFAULTS, "economizer_volume": econ_vol,
                       "ore_expander_cargo": 0.0, "t2_expander_cargo": 0.0})
    return Hauler(p, loaded_ore=3, return_econ=3)


def test_hold_space_is_kept_for_economizers_carried_out():
    h = _hauler(1, 400_000)
    assert h.carried_econ == 3 and h.capacity == 400_000 - 3 * 10_500
    h.loaded_ore = 2                # one low already holds an economizer on the way out
    assert h.carried_econ == 2 and h.capacity == 400_000 - 2 * 10_500
    assert h.fuel_empty < h.fuel_loaded


def test_bulk_haul_waves_split_stacks_and_trim_the_last_wave(world):
    from app.services.jump import Item, plan_haul, plan_route
    jfs = [_hauler(1, 400_000), _hauler(2, 400_000), _hauler(3, 380_000)]
    cap = sum(h.capacity for h in jfs)                     # ~1.11m per wave
    items = [Item("Tritanium", 300_000_000, 0.01),          # 3,000,000 m3, has to be split
             Item("Capital Armor Plates", 30, 10_000),      # 300,000 m3: fits one hold
             Item("Nomad", 1, 1_300_000)]                   # doesn't fit any JF
    routes = {h.profile.character_id: plan_route(world, 30000142, 4, h.profile) for h in jfs}
    plan = plan_haul(items, jfs, routes, round_trip=True)
    assert [i.name for i in plan.too_big] == ["Nomad"]
    assert plan.total_m3 == pytest.approx(3_300_000)
    assert plan.waves == 4 and len(plan.loads) == 10        # 3+3+3 full, then 1 for the rest
    last = [ld for ld in plan.loads if ld.wave == 4]
    assert len(last) == 1                                     # fewest JFs for the remainder
    for ld in plan.loads:
        assert ld.m3 <= ld.hauler.capacity + 1e-6
    moved = sum(q for ld in plan.loads for n, q, _ in ld.items if n == "Tritanium")
    assert moved == 300_000_000                               # nothing lost when splitting
    plates = [q for ld in plan.loads for n, q, _ in ld.items if n == "Capital Armor Plates"]
    assert plates == [30]                                     # kept whole where it fits
    assert plan.total_jumps == 10 * 2 * 2                     # 2 jumps each way per trip
    assert any("Tritanium" in ld.clip() for ld in plan.loads)
    scraps = [m3 for ld in plan.loads for n, _q, m3 in ld.items if m3 < 1000]
    assert len(scraps) <= 1                                   # only the stack's last bit


def test_bulk_haul_never_overfills_overflow_loads():
    from app.services.jump import Item, plan_haul
    # One small JF and a stack of plates that don't pack evenly: whatever doesn't fit
    # the planned waves gets more loads, each within the hold.
    jf = _hauler(1, 25_000, econ_vol=0)
    plan = plan_haul([Item("Capital Armor Plates", 7, 10_000)], [jf])
    assert sum(q for ld in plan.loads for _n, q, _m in ld.items) == 7
    for ld in plan.loads:
        assert ld.m3 <= jf.capacity + 1e-6


def test_haul_page_end_to_end(client, world):
    html = client.post("/jump/haul", data={
        "csrf": "t", "items": "Tritanium\t50,000,000\tMineral\t\t\t500,000 m3",
        "frm": "jita", "to": "ac2e", "reward": "500m", "round_trip": "1", "pilot": "1",
        "ore_1": "3", "econ_1": "3"}).text
    assert "Wave 1" in html and "Wave 2" in html            # 500k m3 over a ~318k hold
    assert "jumps in total" in html and "reward per jump" in html


def test_esi_unreachable_still_plans_a_high_sec_trip(client, monkeypatch):
    """No gate data from ESI (down, or unreachable): still plan, just without gate counts."""
    from app.esi import client as esi

    async def boom(*a, **k):
        raise ConnectionError("ESI unreachable")
    monkeypatch.setattr(esi, "get", boom)
    html = client.get("/jump?tab=route&frm=jita&to=ac2e-3&trip=loaded_empty").text
    assert "AC2E-3 → Jita" in html and "round trip" in html
