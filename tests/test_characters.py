"""Items 2, 3, 5, 8: per-character tracking, personal-only linking, Characters page."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tests.test_db_locking import CHARS, CORP, fake_esi, seeded  # noqa: F401 (fixtures)

NOW = datetime.now(timezone.utc)


def _job(db, jid, owner_type, owner_id):
    from app.models import IndustryJob
    db.add(IndustryJob(job_id=jid, owner_type=owner_type, owner_id=owner_id,
                       installer_id=owner_id, activity_id=1, blueprint_type_id=1,
                       product_type_id=2, runs=1, status="active",
                       start_date=NOW - timedelta(hours=1), end_date=NOW + timedelta(hours=1)))


def test_personal_profile_asks_for_no_corp_scopes():
    from app.esi.sso import CORP_INDUSTRY_SCOPES, INDUSTRY_SCOPES, SCOPE_PROFILES
    personal = set(SCOPE_PROFILES["personal"])
    assert not personal & set(CORP_INDUSTRY_SCOPES)
    assert "esi-contracts.read_corporation_contracts.v1" not in personal
    assert {"esi-skills.read_skills.v1", "esi-industry.read_character_jobs.v1",
            "esi-characters.read_blueprints.v1"} <= personal
    # The director profile still covers everything it did before.
    assert set(CORP_INDUSTRY_SCOPES) <= set(INDUSTRY_SCOPES) <= set(SCOPE_PROFILES["industry"])


def test_missing_scopes_follow_the_toggles(seeded):
    from app.models import Character
    from app.services.characters import ASSETS, missing_scopes
    ch = seeded.get(Character, CHARS[0])           # token has skills/jobs/bps, not assets
    assert missing_scopes(ch) == [ASSETS]
    ch.track_assets = False
    assert missing_scopes(ch) == []                 # switched off -> not needed


def test_industry_off_clears_personal_jobs_but_not_corp_jobs(seeded):
    from app.models import Character, IndustryJob, SyncState
    from app.services.characters import set_tracking
    _job(seeded, 1, "character", CHARS[0])
    _job(seeded, 2, "corporation", CORP)
    seeded.add(SyncState(key=f"jobs:char:{CHARS[0]}", etag='"x"'))
    seeded.commit()

    set_tracking(seeded, seeded.get(Character, CHARS[0]), "industry", False)
    seeded.commit()
    assert [j.job_id for j in seeded.scalars(select(IndustryJob))] == [2]
    assert seeded.get(SyncState, f"jobs:char:{CHARS[0]}") is None  # refetch if re-enabled
    with pytest.raises(ValueError):
        set_tracking(seeded, seeded.get(Character, CHARS[0]), "nonsense", False)


def test_switched_off_characters_are_not_synced(seeded, fake_esi):  # noqa: F811
    from app.models import Character, CharacterSkill
    from app.services.industry import sync_skills
    seeded.get(Character, CHARS[1]).track_skills = False
    seeded.commit()
    fake = fake_esi({"/characters/*/skills": {"skills": [
        {"skill_id": 3380, "trained_skill_level": 5, "active_skill_level": 5}]},
        "/characters/*/skillqueue": []})
    asyncio.run(sync_skills())
    assert not [c for c in fake.calls if str(CHARS[1]) in c]
    seeded.expire_all()
    assert {s.character_id for s in seeded.scalars(select(CharacterSkill))} == {CHARS[0]}


def test_slots_off_hides_character_from_slots_and_home(seeded):
    from app.models import Character, CharacterSkill, EntityName
    from app.services import home, industry
    seeded.add(EntityName(entity_id=3380, name="Industry", category="inventory_type"))
    for cid in CHARS:
        seeded.add(CharacterSkill(character_id=cid, skill_id=3380, trained_level=5,
                                  active_level=5))
    seeded.get(Character, CHARS[0]).track_slots = False
    seeded.commit()
    names = [r.character.character_id for r in industry.character_overview(seeded)]
    assert names == [CHARS[1]]
    assert all(r["character"].character_id != CHARS[0] for r in home.idle_characters(seeded))


def test_relinking_narrower_keeps_the_broader_token(seeded):
    from app.esi.sso import TokenResult
    from app.models import EsiToken
    from app.security import decrypt
    from app.web.routes.auth import save_login
    before = seeded.get(EsiToken, CHARS[0]).scope_set
    tok = TokenResult(character_id=CHARS[0], character_name="Pilot 0", access_token="new",
                      refresh_token="new-r", expires_at=NOW, scopes=["esi-skills.read_skills.v1"])
    saved = save_login(tok, {"corporation_id": CORP}, None, False)
    assert saved.kept_existing
    seeded.expire_all()
    t = seeded.get(EsiToken, CHARS[0])
    assert t.scope_set == before and decrypt(t.refresh_token_enc) == "r"


def test_member_link_from_another_corp_is_listed_but_not_tracked(seeded):
    from app.esi.sso import PERSONAL_SCOPES, TokenResult
    from app.models import Character, Corporation, EntityName
    from app.web.routes.auth import save_login
    tok = TokenResult(character_id=2112000099, character_name="Outsider", access_token="a",
                      refresh_token="r", expires_at=NOW, scopes=list(PERSONAL_SCOPES))
    saved = save_login(tok, {"corporation_id": 98777777},
                       {"name": "Other Corp", "ticker": "OTHR"}, False, "personal")
    assert not saved.kept_existing
    seeded.expire_all()
    ch = seeded.get(Character, 2112000099)
    assert ch.corporation_id == 98777777 and ch.link_role == "member"
    assert seeded.get(EntityName, 98777777).name == "Other Corp"
    corp = seeded.get(Corporation, 98777777)
    assert corp.name == "Other Corp" and not corp.is_tracked and not corp.is_home


def test_sync_info_reports_schedule_and_esi_cache(seeded):
    from app.models import SyncState
    from app.services import sync_info
    seeded.add(SyncState(key="skills:1", expires_at=NOW + timedelta(minutes=2),
                         last_success_at=NOW - timedelta(minutes=1)))
    seeded.add(SyncState(key="bps:char:1", expires_at=NOW - timedelta(minutes=1)))
    seeded.commit()
    feeds = {f.job_id: f for f in sync_info.feeds(seeded, "industry", NOW)}
    assert feeds["industry_skills"].cached_until is not None
    assert feeds["industry_blueprints"].cached_until is None   # expired: next check is fresh
    assert feeds["industry_jobs"].every_text == "every 5 min"
    assert feeds["industry_skills"].every_text == "every hour"
    msg = sync_info.pressed_message(seeded, "industry")
    assert "skills & queues in 1m" in msg and "checking ESI now" in msg


# ------------------------------------------------------------------ pages

@pytest.fixture()
def client(seeded):
    from fastapi.testclient import TestClient
    from starlette.requests import Request

    from app.main import app

    async def fake_login(request: Request):
        request.session["user"] = {"character_id": CHARS[0], "name": "Pilot 0"}
        return {"ok": True}
    app.add_api_route("/__test/login", fake_login)
    c = TestClient(app)
    c.get("/__test/login")
    try:
        yield c
    finally:
        app.router.routes[:] = [r for r in app.router.routes
                                if getattr(r, "path", "") != "/__test/login"]


def _csrf(client):
    import re
    html = client.get("/characters").text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_characters_page_has_toggles_poller_help_and_remove_last(client):
    html = client.get("/characters").text
    for label in ("Skills", "Slots", "Industry", "Assets"):
        assert f"</i>{label}</button>" in html
    assert "Contract poller" in html and "switches which character" in html
    assert "Member" in html and "Director+" in html and "profile=personal" in html
    # Remove is in the last column of the row.
    row = html[html.index(f'id="c{CHARS[1]}"'):]
    row = row[:row.index("</tr>")]
    assert row.rindex("<td") == row.index('<td class="col-remove">')
    assert "Remove</button>" in row


def test_toggle_endpoint_flips_and_redirects_to_the_row(client, seeded):
    from app.models import Character
    r = client.post(f"/characters/{CHARS[1]}/track",
                    data={"csrf": _csrf(client), "what": "slots", "on": "0"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith(f"#c{CHARS[1]}")
    seeded.expire_all()
    assert seeded.get(Character, CHARS[1]).track_slots is False


def test_slots_page_shows_capital_pips_tier_and_sync_help(client, seeded):
    from app.models import CharacterSkill, EntityName
    seeded.add(EntityName(entity_id=22242, name="Capital Ship Construction",
                          category="inventory_type"))
    seeded.add(CharacterSkill(character_id=CHARS[0], skill_id=22242, trained_level=4,
                              active_level=4))
    seeded.commit()
    html = client.get("/industry/slots").text
    assert '<span class="tier tier-4">JF &amp; Supers</span>' in html
    assert 'class="tip capcell"' in html
    assert "What Sync Now does" in html and "Beat ESI" in html


def test_onboarding_remembers_the_personal_profile(client):
    r = client.get("/characters/onboard/start?profile=personal", follow_redirects=False)
    assert r.headers["location"] == "/auth/login?profile=personal"
    html = client.get("/characters/onboard").text
    assert "/auth/login?profile=personal" in html and "Member" in html


def test_remove_asks_for_confirmation_even_with_an_apostrophe_name(client, seeded):
    from app.models import Character
    seeded.get(Character, CHARS[1]).name = "Ma'at O'Neil"
    seeded.commit()
    html = client.get("/characters").text
    assert 'onsubmit="return confirm(this.dataset.confirm)"' in html
    assert "data-confirm=\"Remove Ma&#39;at O&#39;Neil?" in html


def test_remove_clears_personal_jobs_but_keeps_corp_jobs(client, seeded):
    from app.models import Character, IndustryJob
    _job(seeded, 11, "character", CHARS[1])
    _job(seeded, 12, "corporation", CORP)
    seeded.commit()
    r = client.post(f"/characters/{CHARS[1]}/delete", data={"csrf": _csrf(client)},
                    follow_redirects=False)
    assert r.status_code == 303
    seeded.expire_all()
    assert seeded.get(Character, CHARS[1]) is None
    assert [j.job_id for j in seeded.scalars(select(IndustryJob))] == [12]


def test_sync_now_hover_has_live_timer_bars(client, seeded):
    from app.models import SyncState
    seeded.add(SyncState(key="jobs:char:1", expires_at=NOW + timedelta(minutes=3),
                         last_success_at=NOW - timedelta(minutes=2)))
    seeded.commit()
    html = client.get("/industry/jobs").text
    assert ">Sync Now</button>" in html and "Sync now" not in html
    assert 'class="sync-timer esi"' in html and "ESI has new data" in html
    assert "setInterval(tick, 1000)" in html


# ------------------------------------------------------------------ corps + roles

def test_corp_switch_off_clears_corp_data_but_not_personal(seeded):
    from app.models import Blueprint, Corporation, IndustryJob, SyncState
    from app.services.corps import set_tracked
    _job(seeded, 31, "corporation", CORP)
    _job(seeded, 32, "character", CHARS[0])
    seeded.add(SyncState(key=f"jobs:corp:{CORP}", etag='"x"'))
    seeded.commit()
    set_tracked(seeded, seeded.get(Corporation, CORP), False)
    seeded.commit()
    assert [j.job_id for j in seeded.scalars(select(IndustryJob))] == [32]
    assert seeded.get(SyncState, f"jobs:corp:{CORP}") is None
    assert not seeded.get(Corporation, CORP).is_tracked


def test_members_never_read_corp_data_even_with_corp_scopes(seeded, fake_esi):  # noqa: F811
    from app.models import Character, EsiToken
    from app.services.industry import sync_jobs
    t = seeded.get(EsiToken, CHARS[0])
    t.scopes += " esi-industry.read_corporation_jobs.v1"
    seeded.get(Character, CHARS[0]).link_role = "member"
    seeded.commit()
    fake = fake_esi({"/characters/*/industry/jobs": [],
                     f"/corporations/{CORP}/industry/jobs": []})
    asyncio.run(sync_jobs())
    assert not [c for c in fake.calls if c.startswith("/corporations/")]
    seeded.get(Character, CHARS[0]).link_role = "director"
    seeded.commit()
    asyncio.run(sync_jobs())
    assert [c for c in fake.calls if c.startswith("/corporations/")]


def test_member_link_downgrades_a_director_but_plain_sign_in_does_not(seeded):
    from app.esi.sso import PERSONAL_SCOPES, TokenResult
    from app.models import Character, EsiToken
    from app.web.routes.auth import save_login
    from app.esi.sso import SCOPE_PROFILES
    ch = seeded.get(Character, CHARS[0])
    ch.link_role = "director"
    seeded.get(EsiToken, CHARS[0]).scopes = " ".join(SCOPE_PROFILES["industry"])
    seeded.commit()
    narrow = TokenResult(character_id=CHARS[0], character_name="Pilot 0", access_token="n",
                         refresh_token="n-r", expires_at=NOW, scopes=list(PERSONAL_SCOPES))
    assert save_login(narrow, {"corporation_id": CORP}, None, False, "courier").kept_existing
    saved = save_login(narrow, {"corporation_id": CORP}, None, False, "personal")
    assert not saved.kept_existing
    seeded.expire_all()
    assert seeded.get(Character, CHARS[0]).link_role == "member"
    assert seeded.get(EsiToken, CHARS[0]).scope_set == set(PERSONAL_SCOPES)


def test_contracts_come_from_the_home_corp_only(seeded):
    from app.models import Corporation
    from app.services import corps
    seeded.add(Corporation(corporation_id=1, name="Earlier Row", is_tracked=True))
    seeded.commit()
    assert corps.home_corp(seeded).corporation_id == CORP


def test_first_corp_becomes_home_and_later_ones_start_off(db):
    from app.services.corps import ensure_corp
    from app.models import Corporation
    for c in db.scalars(select(Corporation)):
        db.delete(c)
    db.commit()
    first, _ = ensure_corp(db, 111, {"name": "Home"})
    second, _ = ensure_corp(db, 222, {"name": "Other"})
    assert (first.is_home, first.is_tracked) == (True, True)
    assert (second.is_home, second.is_tracked) == (False, False)


def test_characters_page_lists_corps_with_switch_and_role_toggle(client, seeded):
    from app.models import Corporation
    seeded.add(Corporation(corporation_id=98805299, name="Cherry Defence Shipyards",
                           is_tracked=False))
    seeded.commit()
    html = client.get("/characters").text
    assert 'id="corps"' in html and "Cherry Defence Shipyards" in html
    assert '<span class="pill ok small">home</span>' in html
    assert 'action="/corporations/98805299/track"' in html
    assert f'action="/characters/{CHARS[0]}/role"' in html
    r = client.post("/corporations/98805299/track", data={"csrf": _csrf(client), "on": "1"},
                    follow_redirects=False)
    assert r.status_code == 303
    seeded.expire_all()
    assert seeded.get(Corporation, 98805299).is_tracked
    r = client.post(f"/characters/{CHARS[1]}/role", data={"csrf": _csrf(client),
                                                           "role": "director"},
                    follow_redirects=False)
    assert r.status_code == 303
