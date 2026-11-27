"""Items 1, 6, 7: ESI load with many characters, blueprint structures, blueprint categories."""
import asyncio
import fnmatch
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from tests.test_db_locking import ALL_SCOPES, CORP, write_lock_free

NOW = datetime.now(timezone.utc)
STATION = 60003760
STRUCTURE = 1035466617946
OFFICE = 1040000000001
CAN = 1040000000002
DIRECTOR_SCOPES = ALL_SCOPES + " " + " ".join([
    "esi-industry.read_corporation_jobs.v1", "esi-corporations.read_blueprints.v1",
    "esi-assets.read_corporation_assets.v1", "esi-corporations.read_divisions.v1",
    "esi-universe.read_structures.v1", "esi-assets.read_assets.v1"])


class Fake:
    """httpx stand-in: routes are (method, glob) -> body or callable(path, json, token)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str, str | None]] = []
        self.locked: list[str] = []

    async def request(self, method, path, params=None, json=None, headers=None):
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ") or None
        self.calls.append((method, path, token))
        if not write_lock_free():
            self.locked.append(path)
        await asyncio.sleep(0)
        for (m, pattern), body in self.routes.items():
            if m == method and fnmatch.fnmatchcase(path, pattern):
                if callable(body):
                    body = body(path, json, token)
                status = 200
                if isinstance(body, tuple):
                    status, body = body
                return self._resp(method, path, status, body)
        if method == "POST" and path == "/universe/names":
            return self._resp(method, path, 200,
                              [{"id": i, "name": f"Name {i}", "category": "inventory_type"}
                               for i in json])
        return self._resp(method, path, 404, {"error": "not found"})

    @staticmethod
    def _resp(method, path, status, body):
        exp = datetime.now(timezone.utc) + timedelta(minutes=5)
        return httpx.Response(status, json=body, headers={
            "Expires": exp.strftime("%a, %d %b %Y %H:%M:%S GMT"), "ETag": '"e"',
            "X-Ratelimit-Group": "char-industry", "X-Ratelimit-Limit": "600/15m",
            "X-Ratelimit-Remaining": "590"},
            request=httpx.Request(method, f"https://esi.test{path}"))

    def count(self, pattern: str) -> int:
        return sum(1 for _m, p, _t in self.calls if fnmatch.fnmatchcase(p, pattern))


@pytest.fixture()
def fake(monkeypatch):
    from app.esi import client as esi
    from app.esi import usage
    usage.reset()

    def install(routes):
        f = Fake(routes)
        monkeypatch.setattr(esi, "client", lambda: f)
        return f
    return install


def _add_char(db, cid, name, scopes, corp=CORP, **flags):
    from app.models import Character, EsiToken
    from app.security import encrypt
    db.add(Character(character_id=cid, name=name, corporation_id=corp, **flags))
    db.add(EsiToken(character_id=cid, refresh_token_enc=encrypt("r"),
                    access_token_enc=encrypt(f"tok{cid}"),
                    access_expires_at=NOW + timedelta(hours=1), scopes=scopes, is_valid=True))


# ------------------------------------------------------------------ item 1: ESI load

def test_thirty_characters_cost_one_call_each_and_nothing_while_cached(db, fake):
    from app.services.industry import sync_blueprints, sync_jobs, sync_skills
    for i in range(30):
        _add_char(db, 2113000000 + i, f"Pilot {i:02d}", ALL_SCOPES)
    db.commit()
    f = fake({("GET", "/characters/*/skills"): {"skills": []},
              ("GET", "/characters/*/skillqueue"): [],
              ("GET", "/characters/*/industry/jobs"): [],
              ("GET", "/characters/*/blueprints"): []})

    async def one_round():
        await sync_skills()
        await sync_jobs()
        await sync_blueprints()
    asyncio.run(one_round())
    # Exactly one request per character per endpoint: no fan-out, no retries.
    for route in ("skills", "skillqueue", "industry/jobs", "blueprints"):
        assert f.count(f"/characters/*/{route}") == 30, route
    assert len(f.calls) == 120 and f.locked == []

    # Run everything again straight away: ESI's cache hasn't expired, so no calls at all.
    asyncio.run(one_round())
    assert len(f.calls) == 120


def test_expected_calls_per_hour_for_thirty_characters(db):
    from app.services.sync_info import expected_per_hour
    for i in range(30):
        _add_char(db, 2113000000 + i, f"Pilot {i:02d}", ALL_SCOPES)
    db.commit()
    rows = {r.feed: r for r in expected_per_hour(db)}
    assert rows["Industry jobs"].count == 30 and rows["Industry jobs"].per_hour_each == 12
    assert rows["Skills + queue"].per_hour_each == 2
    assert rows["Blueprints"].per_hour_each == 1
    assert rows["Corp contracts"].per_hour == 12      # polled every minute, cached 5
    # Per character: 12 jobs + 2 skills + 1 blueprints = 15/hour, i.e. 1 call every 4
    # minutes, against a per-character bucket of hundreds of tokens per 15 minutes.
    per_char = sum(r.per_hour_each for f, r in rows.items() if r.who == "per character")
    assert per_char == 15


def test_corp_403_falls_back_to_another_director_and_parks_the_first(db, fake):
    from app.models import Character, Corporation, IndustryJob, SyncState
    from app.services.industry import sync_jobs
    _add_char(db, 2114000001, "Member No Roles", DIRECTOR_SCOPES, track_industry=False,
              link_role="director")
    _add_char(db, 2114000002, "Real Director", DIRECTOR_SCOPES, track_industry=False,
              link_role="director")
    db.flush()
    db.get(Corporation, CORP).sync_character_id = 2114000001   # tried first
    db.commit()

    def corp_jobs(path, json, token):
        if token == "tok2114000001":
            return 403, {"error": "Character does not have required role(s)"}
        return [{"job_id": 9, "installer_id": 2114000002, "activity_id": 1,
                 "blueprint_type_id": 1, "product_type_id": 2, "runs": 1, "status": "active",
                 "facility_id": 1, "location_id": STATION,
                 "start_date": "2026-11-27T00:00:00Z", "end_date": "2026-12-27T00:00:00Z"}]
    f = fake({("GET", f"/corporations/{CORP}/industry/jobs"): corp_jobs,
              ("GET", "/universe/types/*"): {"name": "Widget", "group_id": 1},
              ("GET", "/universe/groups/*"): {"name": "Widgets", "category_id": 6}})
    asyncio.run(sync_jobs())
    db.expire_all()
    assert [j.job_id for j in db.scalars(select(IndustryJob))] == [9]
    parked = db.get(SyncState, "denied:esi-industry.read_corporation_jobs.v1:2114000001")
    assert parked and parked.expires_at > NOW + timedelta(hours=5)

    # Next run (cache expired): the member without roles is skipped, no second 403.
    db.get(SyncState, f"jobs:corp:{CORP}").expires_at = NOW - timedelta(seconds=1)
    db.commit()
    before = [t for _m, p, t in f.calls if p.endswith("/industry/jobs")]
    asyncio.run(sync_jobs())
    after = [t for _m, p, t in f.calls if p.endswith("/industry/jobs")][len(before):]
    assert after == ["tok2114000002"]
    assert db.get(Character, 2114000001) is not None


def test_usage_meter_counts_feeds_cached_and_errors():
    import time
    from app.esi import usage
    usage.reset()
    h = {"X-Ratelimit-Group": "char-industry", "X-Ratelimit-Limit": "600/15m",
         "X-Ratelimit-Remaining": "550"}
    usage.record("/characters/1/industry/jobs", 200, h)
    usage.record("/characters/2/industry/jobs", 304, h)
    usage.record("/corporations/9/blueprints", 403, {})
    s = usage.summary()
    assert (s["total"], s["fresh"], s["cached"], s["errors"]) == (3, 1, 1, 1)
    assert {r["family"] for r in s["rows"]} == {"Personal jobs", "Corp blueprints"}
    assert s["tight"][0] == {"group": "char-industry", "remaining": 550, "limit": "600/15m"}
    assert usage.summary(now=time.time() + 4000)["total"] == 0   # rolls off after an hour


# ------------------------------------------------------------------ item 6: structures

def test_walk_and_describe_corp_office_and_container():
    from app.models import AssetNode, Location
    from app.services.places import describe, walk
    nodes = {
        OFFICE: AssetNode(item_id=OFFICE, parent_id=STATION, parent_flag="OfficeFolder",
                          type_id=27, owner_type="corporation", owner_id=CORP),
        CAN: AssetNode(item_id=CAN, parent_id=OFFICE, parent_flag="CorpSAG2", type_id=17366,
                       name="BPO Vault", owner_type="corporation", owner_id=CORP),
    }
    assert walk(CAN, nodes) == (STATION, [nodes[CAN], nodes[OFFICE]])
    locs = {STATION: Location(location_id=STATION, name="Jita IV - Moon 4 - Caldari Navy "
                              "Assembly Plant", system_name="Jita")}
    divs = {"CorpSAG2": "Blueprints", "CorpSAG1": "Main"}
    p = describe(CAN, "Unlocked", nodes, locs, divs)
    assert p.site.startswith("Jita IV") and p.detail == "Blueprints › BPO Vault"
    p = describe(OFFICE, "CorpSAG1", nodes, locs, divs)
    assert p.detail == "Main"
    p = describe(999, "CorpSAG1", {}, {}, divs)
    assert p.site == "Unknown location" and "needs assets access" in p.detail


def test_blueprint_sync_finds_structures_names_containers_and_categories(db, fake):
    from app.models import AssetNode, Location, MarketGroup, TypeInfo
    from app.services.industry import sync_blueprints
    _add_char(db, 2115000001, "Director", DIRECTOR_SCOPES, is_admin=True, track_industry=False,
              link_role="director")
    db.commit()

    def bp(item, type_id, loc, flag):
        return {"item_id": item, "type_id": type_id, "location_id": loc, "location_flag": flag,
                "quantity": -1, "material_efficiency": 10, "time_efficiency": 20, "runs": -1}
    f = fake({
        ("GET", f"/corporations/{CORP}/divisions"):
            {"hangar": [{"division": 1, "name": "Main"}, {"division": 2, "name": "Blueprints"}]},
        ("GET", f"/corporations/{CORP}/blueprints"): [
            bp(1, 1001, CAN, "Unlocked"),          # in a container in the office
            bp(2, 1002, OFFICE, "CorpSAG1"),       # straight in the office hangar
            bp(3, 1001, STRUCTURE, "CorpSAG2"),    # in a structure's corp hangar
        ],
        ("GET", f"/corporations/{CORP}/assets"): [
            {"item_id": OFFICE, "location_id": STATION, "location_flag": "OfficeFolder",
             "location_type": "station", "type_id": 27, "quantity": 1},
            {"item_id": CAN, "location_id": OFFICE, "location_flag": "CorpSAG2",
             "location_type": "item", "type_id": 17366, "quantity": 1},
        ],
        ("POST", f"/corporations/{CORP}/assets/names"):
            lambda p, ids, t: [{"item_id": i, "name": "BPO Vault"} for i in ids],
        ("GET", f"/universe/stations/{STATION}"):
            {"name": "Jita IV - Moon 4 - Caldari Navy Assembly Plant", "system_id": 30000142},
        ("GET", f"/universe/structures/{STRUCTURE}"):
            {"name": "Perimeter - Tranquility Trading Tower", "solar_system_id": 30000144},
        ("GET", "/universe/systems/*"): {"name": "Jita", "security_status": 0.95},
        ("GET", "/universe/types/1001"): {"name": "Rifter Blueprint", "group_id": 105,
                                          "market_group_id": 300},
        ("GET", "/universe/types/1002"): {"name": "Scordite Mining Crystal I Blueprint",
                                          "group_id": 727, "market_group_id": 400},
        ("GET", "/universe/groups/*"): {"name": "Some Blueprint", "category_id": 9},
        ("GET", "/markets/groups/300"): {"name": "Minmatar", "parent_group_id": 200},
        ("GET", "/markets/groups/200"): {"name": "Frigates", "parent_group_id": 100},
        ("GET", "/markets/groups/100"): {"name": "Ships", "parent_group_id": 2},
        ("GET", "/markets/groups/400"): {"name": "Mining Crystals", "parent_group_id": 410},
        ("GET", "/markets/groups/410"): {"name": "Ammunition & Charges", "parent_group_id": 2},
        ("GET", "/markets/groups/2"): {"name": "Blueprints & Reactions"},
    })
    asyncio.run(sync_blueprints())
    assert f.locked == []
    db.expire_all()
    assert db.get(AssetNode, CAN).name == "BPO Vault"
    assert db.get(Location, STATION).name.startswith("Jita IV")
    assert db.get(Location, STRUCTURE).name.startswith("Perimeter")
    assert db.get(TypeInfo, 1001).market_group_id == 300
    assert db.get(MarketGroup, 2).name == "Blueprints & Reactions"

    # Second run: everything placed and categorised -> no assets, types or groups refetched.
    n_assets, n_types = f.count("/corporations/*/assets"), f.count("/universe/types/*")
    asyncio.run(sync_blueprints())
    assert f.count("/corporations/*/assets") == n_assets
    assert f.count("/universe/types/*") == n_types


# ------------------------------------------------------------------ item 7: categories

def test_blueprint_category_mapping():
    from app.web.routes.industry import UNCATEGORIZED, blueprint_category
    assert blueprint_category(["Blueprints & Reactions", "Ships", "Frigates", "Minmatar"]) \
        == ("Ships", "Frigates")
    assert blueprint_category(["Blueprints & Reactions", "Ship Equipment", "Mining Equipment"]) \
        == ("Modules", "Mining Equipment")
    assert blueprint_category(["Blueprints & Reactions", "Ammunition & Charges",
                               "Mining Crystals"]) == ("Ammo & Charges", "Mining Crystals")
    assert blueprint_category([]) == (UNCATEGORIZED, "")


def test_blueprints_page_shows_structure_division_container_and_filters(db, fake):
    from fastapi.testclient import TestClient
    from starlette.requests import Request

    from app.main import app
    test_blueprint_sync_finds_structures_names_containers_and_categories(db, fake)

    async def fake_login(request: Request):
        request.session["user"] = {"character_id": 2115000001, "name": "Director"}
        return {"ok": True}
    app.add_api_route("/__test/login2", fake_login)
    try:
        c = TestClient(app)
        c.get("/__test/login2")
        html = c.get("/industry/blueprints").text
        assert "Jita IV - Moon 4 - Caldari Navy Assembly Plant" in html
        assert "Blueprints › BPO Vault" in html
        assert "Perimeter - Tranquility Trading Tower" in html
        assert "Division 1" not in html
        assert ">Ships <span class=\"tab-n\">" in html and "Ammo &amp; Charges" in html

        only = c.get("/industry/blueprints?cat=Ammo+%26+Charges").text
        # (Names come from the fake /universe/names: type 1002 is the crystal, 1001 the Rifter.)
        assert "Name 1002" in only
        assert "Name 1001" not in only           # the Rifter is filtered out
        assert "Mining Crystals" in only
    finally:
        app.router.routes[:] = [r for r in app.router.routes
                                if getattr(r, "path", "") != "/__test/login2"]
