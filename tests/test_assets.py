"""Assets + Materials: placement, sync (incl. volume), watches, targets, pages."""
import asyncio
import time
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from tests.test_blueprints_esi import (  # noqa: F401 (fixture)
    CAN, CORP, DIRECTOR_SCOPES, NOW, OFFICE, STATION, STRUCTURE, _add_char, fake,
)

TATARA = 1040111111111
SOTIYO = 1040222222222
INNER = 1040000000009


def _a(item, type_id, loc, flag, qty=1, singleton=False):
    return {"item_id": item, "type_id": type_id, "location_id": loc, "location_flag": flag,
            "location_type": "item", "quantity": qty, "is_singleton": singleton}


CORP_ASSETS = [
    _a(OFFICE, 27, STATION, "OfficeFolder", singleton=True),           # NPC station office
    _a(CAN, 17366, OFFICE, "CorpSAG3", singleton=True),                 # can in division 3
    _a(INNER, 17366, CAN, "Unlocked", singleton=True),                  # can inside the can
    _a(10, 34, INNER, "Unlocked", 5_000_000),                           # Tritanium, nested
    _a(11, 34, OFFICE, "CorpSAG1", 1_000_000),                          # Tritanium, division 1
    _a(12, 16672, SOTIYO, "CorpSAG2", 400),                             # Tenebrex? in structure
    _a(13, 21027, TATARA, "CorpSAG4", 90),                              # Capital part in Tatara
]


# ------------------------------------------------------------------ placement

def test_place_rows_works_out_site_division_and_container_path():
    from app.services.assets import place_rows
    rows = {r["item_id"]: r for r in place_rows("corporation", CORP, CORP_ASSETS)}
    assert rows[10]["site_id"] == STATION and rows[10]["division"] == "CorpSAG3"
    assert rows[10]["container_id"] == INNER
    assert rows[10]["container_path"] == f",{CAN},{INNER},"
    assert (rows[11]["site_id"], rows[11]["division"], rows[11]["container_id"]) == \
        (STATION, "CorpSAG1", None)
    assert (rows[12]["site_id"], rows[12]["division"]) == (SOTIYO, "CorpSAG2")
    assert rows[CAN]["division"] == "CorpSAG3" and rows[CAN]["container_path"] == ""


def test_place_rows_personal_hangar_and_a_fitted_ship():
    from app.services.assets import place_rows
    ship = 1050000000001
    raw = [_a(ship, 28844, TATARA, "Hangar", singleton=True),      # a Rhea
           _a(20, 33400, ship, "LoSlot0", singleton=True),          # fitted module
           _a(21, 34, TATARA, "Hangar", 500)]
    rows = {r["item_id"]: r for r in place_rows("character", 1, raw)}
    assert rows[20]["site_id"] == TATARA and rows[20]["container_id"] == ship
    assert rows[21]["site_id"] == TATARA and rows[21]["container_id"] is None


# ------------------------------------------------------------------ sync

def _routes(personal):
    from tests.test_blueprints_esi import STATION as ST
    return {
        ("GET", f"/corporations/{CORP}/assets"): CORP_ASSETS,
        ("GET", "/characters/*/assets"): personal,
        ("POST", f"/corporations/{CORP}/assets/names"):
            lambda p, ids, t: [{"item_id": i, "name": {CAN: "Minerals", INNER: "Overflow"}.get(i, "None")}
                               for i in ids],
        ("POST", "/characters/*/assets/names"): lambda p, ids, t: [],
        ("GET", f"/universe/stations/{ST}"): {"name": "Some Station", "system_id": 30000142},
        ("GET", f"/universe/structures/{SOTIYO}"): {"name": "AC2E-3 - Sotiyo", "solar_system_id": 30002001},
        ("GET", f"/universe/structures/{TATARA}"): {"name": "AC2E-3 - Tatara", "solar_system_id": 30002001},
        ("GET", "/universe/systems/*"): {"name": "AC2E-3", "security_status": -0.4},
        ("GET", "/universe/types/34"): {"name": "Tritanium", "group_id": 18},
        ("GET", "/universe/types/*"): {"name": "Thing", "group_id": 1},
        ("GET", "/universe/groups/18"): {"name": "Mineral", "category_id": 4},
        ("GET", "/universe/groups/*"): {"name": "Stuff", "category_id": 6},
    }


def test_sync_stores_corp_and_personal_assets_with_names_and_no_lock(db, fake):
    """Also: the same item in two owners' lists (moved between syncs) doesn't break it."""
    from app.models import Asset, AssetNode, Location
    from app.services.assets import sync_assets
    _add_char(db, 2116000001, "CEO", DIRECTOR_SCOPES, is_admin=True, link_role="director")
    _add_char(db, 2116000002, "Builder", DIRECTOR_SCOPES, track_industry=False)  # a Member
    db.commit()
    f = fake(_routes([_a(30, 34, TATARA, "Hangar", 777)]))
    asyncio.run(sync_assets())
    assert f.locked == []
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Asset).where(
        Asset.owner_type == "corporation")) == len(CORP_ASSETS)
    # Both pilots' lists contain the same item (the fake returns one list for both): the
    # later list wins instead of failing, so it's stored once.
    assert db.scalar(select(func.count()).select_from(Asset).where(
        Asset.owner_type == "character")) == 1
    assert db.get(AssetNode, CAN).name == "Minerals"
    assert db.get(Location, SOTIYO).name == "AC2E-3 - Sotiyo"

    calls = len(f.calls)
    asyncio.run(sync_assets())          # cache still valid: no asset calls again
    assert not [c for c in f.calls[calls:] if c[1].endswith("/assets")]


def test_sync_handles_a_large_asset_list_quickly(db, fake):
    """~30k stacks over 3 pages (a big corp) stores in one go without slowing down."""
    from app.models import Asset
    from app.services.assets import sync_assets
    _add_char(db, 2116000001, "CEO", DIRECTOR_SCOPES, is_admin=True, link_role="director",
              track_assets=False)
    db.commit()
    big = [_a(OFFICE, 27, STATION, "OfficeFolder", singleton=True)] + \
          [_a(2_000_000 + i, 34 + (i % 400), OFFICE, f"CorpSAG{1 + i % 7}", 10 + i)
           for i in range(30_000)]
    pages = [big[i:i + 10_000] for i in range(0, len(big), 10_000)]

    def corp_assets(path, json, token):
        return pages[0]
    routes = _routes([])
    routes[("GET", f"/corporations/{CORP}/assets")] = corp_assets
    f = fake(routes)
    from app.esi import client as esi

    async def paged(db_, key, path, token=None, params=None, paged=True):  # 3 pages of data
        await f.request("GET", path)
        return esi.EsiResult(data=big, not_modified=False, expires_at=None)
    import app.services.industry as ind
    orig = esi.get_cached_paged
    esi.get_cached_paged = paged
    try:
        t0 = time.monotonic()
        asyncio.run(sync_assets())
        took = time.monotonic() - t0
    finally:
        esi.get_cached_paged = orig
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Asset)) == len(big)
    assert took < 20, took
    assert ind is not None


# ------------------------------------------------------------------ watches + materials

def _store(db):
    from app.services.assets import store
    store(db, "corporation", CORP, CORP_ASSETS)
    store(db, "character", 7, [_a(40, 34, TATARA, "Hangar", 100)])
    db.commit()


def test_watch_kinds_select_the_right_stacks(db):
    from app.models import Asset
    from app.services.assets import toggle_watch, watch_filter
    _store(db)

    def watched():
        return sorted(a.item_id for a in db.scalars(select(Asset).where(watch_filter(db))))
    assert watched() == []
    assert toggle_watch(db, "container", CAN, label="Minerals")
    db.commit()
    assert watched() == [10, INNER]                         # everything inside, nested too
    toggle_watch(db, "division", STATION, CORP, "CorpSAG1")
    db.commit()
    assert 11 in watched()
    toggle_watch(db, "personal", TATARA, 7)
    db.commit()
    assert 40 in watched() and 13 not in watched()           # not the corp's Tatara stock
    assert not toggle_watch(db, "container", CAN)            # second click unwatches
    db.commit()
    assert 10 not in watched()


def test_materials_totals_targets_and_lists(db):
    from app.models import AssetWatch, MaterialTarget, TypeInfo
    from app.services.assets import clipboard_text
    from app.web.routes.assets import material_lines
    _store(db)
    db.add(TypeInfo(type_id=34, name="Tritanium", group_id=18, group_name="Mineral",
                    category_id=4, market_group_id=1))
    db.add(TypeInfo(type_id=21027, name="Capital Armor Plates", group_name=
                    "Capital Construction Components", category_id=17, market_group_id=1))
    db.add(AssetWatch(kind="site", target_id=STATION))
    db.add(AssetWatch(kind="site", target_id=TATARA))
    db.add(MaterialTarget(type_id=34, min_qty=10_000_000, max_qty=None))
    db.add(MaterialTarget(type_id=21027, min_qty=None, max_qty=50))
    db.commit()
    sections, every = material_lines(db, "watched")
    # Items whose group isn't looked up yet still show, at the end, so nothing hides.
    assert list(sections) == ["Minerals", "Capital components", "Not categorized yet"]
    trit = next(ln for ln in every if ln.type_id == 34)
    assert trit.quantity == 6_000_100            # 5m nested + 1m div 1 + 100 personal
    assert trit.short == 3_999_900
    caps = next(ln for ln in every if ln.type_id == 21027)
    assert caps.excess == 40
    assert clipboard_text([("Tritanium", 3_999_900), ("Zero", 0)]) == "Tritanium 3999900"


# ------------------------------------------------------------------ pages

@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from app.main import app

    async def login(request: Request):
        request.session["user"] = {"character_id": 1, "name": "Boss"}
        request.session["csrf"] = "t"
        return {}
    app.add_api_route("/__test/login6", login)
    c = TestClient(app)
    c.get("/__test/login6")
    yield c
    app.router.routes[:] = [r for r in app.router.routes
                            if getattr(r, "path", "") != "/__test/login6"]


def test_assets_page_search_watch_and_export(client, db):
    from app.models import AssetWatch, EntityName, Location
    _store(db)
    db.add(EntityName(entity_id=34, name="Tritanium", category="inventory_type"))
    db.add(Location(location_id=TATARA, name="AC2E-3 - Tatara", system_id=30002001,
                    system_name="AC2E-3", kind="structure"))
    db.commit()
    html = client.get("/industry/assets?q=trit").text
    assert "Tritanium" in html and "6,000,100" in html
    assert "AC2E-3 - Tatara" in html and "📦 Minerals" not in html   # name not fetched yet
    r = client.post("/industry/assets/watch", data={
        "csrf": "t", "kind": "system", "target": 30002001, "label": "AC2E-3",
        "back": "/industry/assets"}, follow_redirects=False)
    assert r.status_code == 303 and db.scalar(select(func.count()).select_from(AssetWatch)) == 1
    txt = client.get("/industry/assets/export.txt?watched=1").text
    # Watching AC2E-3 covers everything in its structures: corp and personal stock.
    assert txt.splitlines() == ["Tritanium 100", "Type 21027 90"]


def test_materials_page_and_saving_targets_with_k_m_suffixes(client, db):
    from app.models import AssetWatch, MaterialTarget, TypeInfo
    _store(db)
    db.add(TypeInfo(type_id=34, name="Tritanium", group_name="Mineral", category_id=4,
                    market_group_id=1))
    db.add(AssetWatch(kind="site", target_id=STATION))
    db.commit()
    html = client.get("/industry/materials").text
    assert "Minerals" in html and 'name="min_34"' in html
    r = client.post("/industry/materials/targets",
                    data={"csrf": "t", "min_34": "10m", "max_34": "2.5b", "scope": "watched"},
                    follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    t = db.get(MaterialTarget, 34)
    assert (t.min_qty, t.max_qty) == (10_000_000, 2_500_000_000)
    html = client.get("/industry/materials").text
    assert "buy 4,000,000" in html
    assert client.get("/industry/materials/export.txt?list=shopping").text == "Tritanium 4000000"
