"""Item 9: "database is locked".

The bug: syncs wrote to SQLite and then waited on ESI while still holding the write lock.
Anything else that tried to write froze the shared event loop waiting for that lock, so
the sync could never finish and release it -> "database is locked" -> 500 page.

The core regression test runs each sync against a fake ESI that, at every single
network call, checks from a separate connection that the write lock is free.
"""
import asyncio
import fnmatch
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

CORP = 98000001
CHARS = (2112000001, 2112000002)
ALL_SCOPES = " ".join([
    "esi-skills.read_skills.v1", "esi-skills.read_skillqueue.v1",
    "esi-industry.read_character_jobs.v1", "esi-characters.read_blueprints.v1",
    "esi-contracts.read_corporation_contracts.v1",
])


# ------------------------------------------------------------------ helpers

def _db_path() -> str:
    from app.db import engine
    return engine.url.database


def write_lock_free(wait: float = 1.0) -> bool:
    """Can another connection take the write lock within `wait` seconds?

    The probe runs on the event loop's thread, like the old failing web request did, so
    a holder that's parked on an await can never release in time (the bug), while a
    worker thread's short transaction (legit, e.g. a login being saved) can."""
    con = sqlite3.connect(_db_path(), timeout=wait)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class FakeESI:
    """Stands in for the httpx client, so the real ESI code (and its lock guard) runs."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []
        self.locked_during: list[str] = []

    async def request(self, method, path, params=None, json=None, headers=None):
        self.calls.append(path)
        if not write_lock_free():
            self.locked_during.append(f"{method} {path}")
        await asyncio.sleep(0)  # a real request yields to the event loop here
        if method == "POST" and path == "/universe/names":
            body = [{"id": i, "name": f"Name {i}", "category": "character"} for i in json]
            return self._resp(method, path, 200, body)
        for pattern, body in self.routes.items():
            if fnmatch.fnmatchcase(path, pattern):
                return self._resp(method, path, 200, body)
        return self._resp(method, path, 404, {"error": "not found"})

    @staticmethod
    def _resp(method, path, status, body):
        exp = datetime.now(timezone.utc) + timedelta(minutes=5)
        return httpx.Response(status, json=body, headers={
            "Expires": exp.strftime("%a, %d %b %Y %H:%M:%S GMT"), "ETag": '"abc"'},
            request=httpx.Request(method, f"https://esi.test{path}"))


@pytest.fixture()
def seeded(db):
    from app.models import Character, Corporation, EsiToken
    from app.security import encrypt
    for i, cid in enumerate(CHARS):
        db.add(Character(character_id=cid, name=f"Pilot {i}", corporation_id=CORP,
                         is_admin=(i == 0)))
        db.add(EsiToken(character_id=cid, refresh_token_enc=encrypt("r"),
                        access_token_enc=encrypt("a"),
                        access_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                        scopes=ALL_SCOPES, is_valid=True))
    db.get(Corporation, CORP).sync_character_id = CHARS[0]
    db.commit()
    return db


@pytest.fixture()
def fake_esi(monkeypatch):
    from app.esi import client as esi

    def install(routes):
        fake = FakeESI(routes)
        monkeypatch.setattr(esi, "client", lambda: fake)
        return fake
    return install


# ------------------------------------------------------------------ guard unit tests

def test_release_write_lock_only_commits_the_current_tasks_session(db):
    from app.db import SessionLocal, holds_write_lock, release_write_lock
    from app.models import Setting

    async def scenario():
        s = SessionLocal()
        s.get(Setting, "k")
        assert not holds_write_lock(s)          # reads never hold the lock
        s.add(Setting(key="k", value="1"))
        assert not holds_write_lock(s)          # pending, not flushed: no lock yet
        s.flush()
        assert holds_write_lock(s) and not write_lock_free(wait=0)

        async def someone_else():
            return release_write_lock()
        assert await asyncio.create_task(someone_else()) == 0  # not theirs to commit
        assert holds_write_lock(s)

        assert release_write_lock() == 1
        assert not holds_write_lock(s) and write_lock_free(wait=0)
        s.close()

    asyncio.run(scenario())
    db.expire_all()
    from app.models import Setting as S
    assert db.get(S, "k").value == "1"


def test_is_locked_error_recognises_wrapped_and_raw_errors():
    from app.db import is_locked_error
    raw = sqlite3.OperationalError("database is locked")
    assert is_locked_error(raw)
    assert is_locked_error(OperationalError("INSERT", {}, raw))
    assert not is_locked_error(OperationalError("SELECT", {}, sqlite3.OperationalError(
        "no such table: x")))
    assert not is_locked_error(ValueError("locked"))


def test_with_retry_retries_busy_then_succeeds_and_gives_up_on_other_errors():
    from app.db import with_retry
    tries = []

    def flaky():
        tries.append(1)
        if len(tries) < 3:
            raise OperationalError("UPDATE", {}, sqlite3.OperationalError("database is locked"))
        return "saved"
    assert with_retry(flaky, delay=0) == "saved" and len(tries) == 3

    def broken():
        raise OperationalError("SELECT", {}, sqlite3.OperationalError("no such column"))
    with pytest.raises(OperationalError):
        with_retry(broken, delay=0)


# ------------------------------------------------------------------ the regression tests

def test_skill_sync_never_holds_the_lock_while_waiting_on_esi(seeded, fake_esi):
    from app.models import CharacterSkill
    from app.services.industry import sync_skills
    fake = fake_esi({
        "/characters/*/skills": {"skills": [
            {"skill_id": 3380, "trained_skill_level": 5, "active_skill_level": 5},
            {"skill_id": 45746, "trained_skill_level": 4, "active_skill_level": 4}]},
        "/characters/*/skillqueue": [],
    })
    asyncio.run(sync_skills())
    assert fake.calls, "fake ESI was never called"
    assert fake.locked_during == []
    seeded.expire_all()
    assert len(seeded.scalars(select(CharacterSkill)).all()) == 4  # 2 skills x 2 pilots


def test_job_sync_never_holds_the_lock_while_waiting_on_esi(seeded, fake_esi):
    from app.models import IndustryJob
    from app.services.industry import sync_jobs
    now = datetime.now(timezone.utc)

    def job(jid):
        return {"job_id": jid, "installer_id": CHARS[0], "activity_id": 1,
                "blueprint_type_id": 1000 + jid, "product_type_id": 2000 + jid, "runs": 1,
                "status": "active", "facility_id": 1, "output_location_id": 60003760,
                "start_date": _iso(now - timedelta(hours=1)), "end_date": _iso(now + timedelta(hours=5))}
    fake = fake_esi({
        f"/characters/{CHARS[0]}/industry/jobs": [job(1), job(2)],
        f"/characters/{CHARS[1]}/industry/jobs": [job(3)],
        "/universe/types/*": {"name": "Widget", "group_id": 7},
        "/universe/groups/*": {"name": "Widgets", "category_id": 6},
    })
    asyncio.run(sync_jobs())
    assert fake.locked_during == []
    seeded.expire_all()
    assert len(seeded.scalars(select(IndustryJob)).all()) == 3


def test_blueprint_sync_never_holds_the_lock_while_waiting_on_esi(seeded, fake_esi):
    from app.models import Blueprint
    from app.services.industry import sync_blueprints

    def bp(item):
        return {"item_id": item, "type_id": 690 + item % 3, "location_id": 60003760,
                "location_flag": "Hangar", "quantity": -1, "material_efficiency": 10,
                "time_efficiency": 20, "runs": -1}
    fake = fake_esi({
        f"/characters/{CHARS[0]}/blueprints": [bp(i) for i in range(1, 30)],
        f"/characters/{CHARS[1]}/blueprints": [bp(i) for i in range(100, 110)],
    })
    asyncio.run(sync_blueprints())
    assert fake.locked_during == []
    seeded.expire_all()
    assert len(seeded.scalars(select(Blueprint)).all()) == 39


def _contract_routes(contracts):
    return {
        f"/corporations/{CORP}/contracts": contracts,
        "/universe/stations/60003760": {"name": "Jita IV - Moon 4", "system_id": 30000142},
        "/universe/systems/30000142": {"name": "Jita", "security_status": 0.95},
        "/universe/structures/*": {"name": "Some Keepstar", "solar_system_id": 30000142},
    }


def test_contract_sync_never_holds_the_lock_and_still_alerts(seeded, fake_esi):
    from app.models import Contract, Customer, Location, Notification
    from app.services.contract_sync import sync_corporation_contracts
    from tests.test_contract_sync import raw_contract

    fake = fake_esi(_contract_routes([raw_contract(contract_id=1), raw_contract(
        contract_id=2, issuer_id=90000002)]))
    asyncio.run(sync_corporation_contracts())
    assert fake.locked_during == []

    seeded.expire_all()
    assert {c.contract_id for c in seeded.scalars(select(Contract))} == {1, 2}
    assert seeded.get(Customer, 90000001).name == "Name 90000001"
    assert seeded.get(Location, 60003760).system_name == "Jita"
    kinds = [n.kind for n in seeded.scalars(select(Notification))]
    assert kinds.count("new_contract") == 2


def test_contract_sync_skips_location_lookups_for_a_spam_flood(seeded, fake_esi):
    """The spam check now runs before anything is stored; a flood still costs no lookups."""
    from app.models import Customer, Notification
    from app.services.contract_sync import sync_corporation_contracts
    from tests.test_contract_sync import raw_contract

    issued = _iso(datetime.now(timezone.utc) - timedelta(minutes=10))
    flood = [raw_contract(contract_id=500 + i, issuer_id=90000666, date_issued=issued,
                          start_location_id=60000001 + i) for i in range(12)]
    fake = fake_esi(_contract_routes(flood))
    asyncio.run(sync_corporation_contracts())
    assert not [c for c in fake.calls if c.startswith("/universe/stations/600000")]
    seeded.expire_all()
    assert seeded.get(Customer, 90000666).muted
    kinds = [n.kind for n in seeded.scalars(select(Notification))]
    assert "spam_detected" in kinds and "new_contract" not in kinds


def test_contract_sync_failure_stores_nothing_and_refetches_next_time(seeded, fake_esi,
                                                                      monkeypatch):
    """All-or-nothing: if storing fails, no half-saved contracts and no kept ETag."""
    from app.models import Contract, SyncState
    from app.services import contract_sync
    from tests.test_contract_sync import raw_contract

    fake_esi(_contract_routes([raw_contract(contract_id=7)]))

    def boom(*_a, **_k):
        raise RuntimeError("simulated")
    monkeypatch.setattr(contract_sync, "detect_spam", boom)
    asyncio.run(contract_sync.sync_corporation_contracts())

    seeded.expire_all()
    assert seeded.scalars(select(Contract)).all() == []
    state = seeded.get(SyncState, f"corp_contracts:{CORP}")
    assert state.etag is None and state.expires_at is None
    assert "simulated" in state.last_error


def test_web_write_is_not_starved_while_a_sync_runs(seeded, fake_esi):
    """The original failure, end to end: a sync is mid-flight on the event loop and a
    login writes at the same moment. Before the fix this raised 'database is locked'."""
    from app.services.industry import sync_blueprints
    from app.web.routes.auth import save_login
    from app.esi.sso import TokenResult

    fake = fake_esi({f"/characters/{c}/blueprints": [
        {"item_id": c + i, "type_id": 690, "location_id": 1, "location_flag": "Hangar",
         "quantity": -1, "material_efficiency": 0, "time_efficiency": 0, "runs": -1}
        for i in range(50)] for c in CHARS})
    tok = TokenResult(character_id=2123950748, character_name="New Pilot", access_token="a",
                      refresh_token="r", expires_at=datetime.now(timezone.utc)
                      + timedelta(minutes=20), scopes=["publicData"])

    async def scenario():
        sync = asyncio.create_task(sync_blueprints())
        await asyncio.sleep(0)  # let the sync start and reach its first ESI wait
        # Same thing the login callback does: write on the event loop's thread pool.
        saved = await asyncio.to_thread(save_login, tok, {"corporation_id": 1}, None, False)
        await sync
        return saved.name

    t0 = time.monotonic()
    assert asyncio.run(scenario()) == "New Pilot"
    assert time.monotonic() - t0 < 5  # no lock wait
    assert fake.locked_during == []


def test_login_save_waits_out_a_short_busy_moment(seeded):
    """Another writer holds the lock briefly; the login save waits and lands."""
    from app.db import with_retry
    from app.esi.sso import TokenResult
    from app.models import Character
    from app.web.routes.auth import save_login

    tok = TokenResult(character_id=2123950749, character_name="Retry Pilot", access_token="a",
                      refresh_token="r", expires_at=datetime.now(timezone.utc), scopes=[])
    hold = sqlite3.connect(_db_path(), check_same_thread=False)
    hold.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(0.5, hold.rollback)
    timer.start()
    try:
        assert with_retry(lambda: save_login(tok, {}, None, False)).name == "Retry Pilot"
    finally:
        timer.join()
        hold.close()
    seeded.expire_all()
    assert seeded.get(Character, 2123950749) is not None


# ------------------------------------------------------------------ error pages

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    def locked():
        raise OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))

    def broken():
        raise RuntimeError("kaboom")
    app.add_api_route("/__test/locked", locked, methods=["GET", "POST"])
    app.add_api_route("/__test/broken", broken)
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.router.routes[:] = [r for r in app.router.routes
                                if not getattr(r, "path", "").startswith("/__test/")]


def test_busy_database_shows_friendly_page_with_retry(client):
    r = client.get("/__test/locked?x=1", headers={"accept": "text/html",
                                                  "referer": "http://testserver/industry/jobs"})
    assert r.status_code == 503
    assert "The database was busy" in r.text
    assert 'href="/__test/locked?x=1"' in r.text      # Try again = same page
    assert 'href="/industry/jobs"' in r.text           # Go back = where you came from
    assert "Internal Server Error" not in r.text


def test_busy_database_on_a_form_post_says_nothing_was_saved(client):
    r = client.post("/__test/locked", headers={"accept": "text/html"})
    assert r.status_code == 503
    assert "safe to submit again" in r.text
    assert "Try again" not in r.text  # never silently re-submit a form


def test_unexpected_error_shows_friendly_page_not_bare_500(client):
    r = client.get("/__test/broken", headers={"accept": "text/html",
                                              "referer": "https://evil.example/x"})
    assert r.status_code == 500
    assert "Something went wrong" in r.text and "RuntimeError" in r.text
    assert 'href="https://evil.example' not in r.text  # back link stays on this site


def test_json_clients_get_json_errors(client):
    r = client.get("/__test/locked", headers={"accept": "application/json"})
    assert r.status_code == 503 and r.json()["error"] == "The database was busy"
