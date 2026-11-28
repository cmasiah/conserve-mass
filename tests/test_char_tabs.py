"""Characters page tabs: Implants, Skills, Queues."""
import asyncio
from datetime import datetime, timedelta, timezone

from tests.test_characters import client  # noqa: F401 (fixture)
from tests.test_db_locking import CHARS, seeded  # noqa: F401 (fixtures)

NOW = datetime.now(timezone.utc)


def _seed_info(db):
    from app.models import CharacterSkill, EntityName, Location
    from app.services import charinfo
    from app.esi.sso import CLONE_SCOPES
    from app.models import Character
    a, b = CHARS
    ch = db.get(Character, a)
    ch.token.scopes = " ".join(ch.token.scope_set | set(CLONE_SCOPES))
    db.add(EntityName(entity_id=10216, name="Ocular Filter - Basic", category="inventory_type"))
    db.add(EntityName(entity_id=3300, name="Gunnery", category="inventory_type"))
    db.add(Location(location_id=60003760, kind="station", name="Jita IV - Moon 4"))
    db.add(CharacterSkill(character_id=a, skill_id=3300, trained_level=5, active_level=5))
    db.add(CharacterSkill(character_id=a, skill_id=33399, trained_level=4, active_level=4))
    charinfo.update(db, a, total_sp=12_345_678, unallocated_sp=250_000,
                    implants=[10216],
                    attributes={"intelligence": 27, "memory": 21, "perception": 20,
                                "willpower": 20, "charisma": 19, "bonus_remaps": 2,
                                "accrued_remap_cooldown_date": (NOW - timedelta(days=1)).isoformat()},
                    clones={"home_location": {"location_id": 60003760},
                            "last_clone_jump_date": (NOW - timedelta(hours=10)).isoformat(),
                            "jump_clones": [{"location_id": 60003760, "implants": [10216],
                                             "name": "Trade clone"}]},
                    queue=[{"skill_id": 3300, "level": 5,
                            "start": (NOW - timedelta(hours=1)).isoformat(),
                            "finish": (NOW + timedelta(hours=3)).isoformat()}])
    db.commit()


def test_tabs_show_implants_remaps_and_clones(client, seeded):
    _seed_info(seeded)
    html = client.get("/characters?tab=implants").text
    for tab in ("Characters", "Implants", "Skills", "Queues"):
        assert f">{tab}</a>" in html
    assert "Ocular Filter - Basic" in html and "/10" in html
    assert "Int</span> 27" in html and "Yearly remap ready" in html and "2 bonus remaps" in html
    assert "Jita IV - Moon 4" in html and "1 jump clone" in html and "Trade clone" in html
    # 24h - 4h (Infomorph Synchronizing IV) = 20h; jumped 10h ago, so 10h to go.
    assert "jump in 9h" in html or "jump in 10h" in html
    assert "re-link to see implants" in html      # the second character's old token
    assert "developers.eveonline.com" in html and "clones=1" in html
    # The Characters list itself isn't on this tab.
    assert "Contract poller" not in html


def test_skills_and_queue_tabs(client, seeded):
    _seed_info(seeded)
    html = client.get("/characters?tab=skills").text
    assert "12,345,678" in html and "250,000" in html
    html = client.get("/characters?tab=queues").text
    assert "Gunnery V" in html and "queue-meter" in html
    assert "Queue empty" in html            # the second character has nothing queued


def test_list_tab_is_the_default(client, seeded):
    html = client.get("/characters").text
    assert "Contract poller" in html and 'class="active">Characters</a>' in html


def test_clone_scopes_are_opt_in_until_a_link_proves_the_app_has_them(client, seeded,
                                                                     monkeypatch):
    """EVE rejects the whole login if the app lacks a requested scope, so plain links
    must not ask for them until one 'with implants' link has worked."""
    from urllib.parse import parse_qs, urlparse

    from app.esi.sso import CLONE_SCOPES, CLONES_OK_KEY, PERSONAL_SCOPES, SCOPE_PROFILES
    from app.models import Setting
    for p in ("personal", "industry"):
        assert not set(CLONE_SCOPES) & set(SCOPE_PROFILES[p])
    assert not set(CLONE_SCOPES) & set(PERSONAL_SCOPES)

    from app.web.routes import auth
    monkeypatch.setattr(auth, "get_settings",
                        lambda: type("S", (), {"missing_required": lambda self: []})())

    def asked(url):
        loc = client.get(url, follow_redirects=False).headers["location"]
        return set(parse_qs(urlparse(loc).query)["scope"][0].split())
    assert not set(CLONE_SCOPES) & asked("/auth/login?profile=personal")
    assert set(CLONE_SCOPES) <= asked("/auth/login?profile=personal&clones=1")
    seeded.add(Setting(key=CLONES_OK_KEY, value="1")); seeded.commit()
    assert set(CLONE_SCOPES) <= asked("/auth/login?profile=industry")
    assert not set(CLONE_SCOPES) & asked("/auth/login?profile=courier")


def test_sync_stores_attributes_implants_and_clones(seeded, monkeypatch):
    from app.esi import client as esi
    from app.esi import resolver
    from app.models import Character
    from app.services import charinfo
    for ch in seeded.query(Character):
        ch.token.scopes = " ".join(ch.token.scope_set | {charinfo.SCOPE_IMPLANTS,
                                                         charinfo.SCOPE_CLONES,
                                                         charinfo.SCOPE_SKILLS})
    seeded.commit()

    class Res:
        def __init__(self, data):
            self.data, self.not_modified = data, False

    async def cached(db, key, path, token=None, paged=True):
        if path.endswith("/attributes"):
            return Res({"intelligence": 24, "bonus_remaps": 1})
        if path.endswith("/implants"):
            return Res([10216])
        return Res({"jump_clones": []})

    async def tok(db, cid):
        return "t"

    async def names(db, ids):
        return {}
    monkeypatch.setattr(esi, "get_cached_paged", cached)
    monkeypatch.setattr(esi, "access_token", tok)
    monkeypatch.setattr(resolver, "resolve_names", names)
    asyncio.run(charinfo.sync_character_info())
    info = charinfo.get(seeded, CHARS[0])
    assert info["attributes"]["intelligence"] == 24 and info["implants"] == [10216]


def test_characters_page_has_sync_now_that_comes_back_here(client, seeded):
    html = client.get("/characters?tab=implants").text
    assert 'action="/industry/sync-now"' in html and 'value="/characters?tab=implants#chars"' in html
    r = client.post("/industry/sync-now", data={"csrf": client.cookies.get("csrf") or "",
                                               "back": "/characters?tab=implants#chars"},
                    follow_redirects=False)
    if r.status_code == 303:                       # csrf accepted in this test setup
        assert r.headers["location"].startswith("/characters")


def test_system_sync_everything_runs_both_and_stays_on_system(monkeypatch):
    from app.jobs import scheduler
    from app.web.routes import pages
    ran = []
    monkeypatch.setattr(scheduler, "trigger_sync_now", lambda: ran.append("contracts"))
    monkeypatch.setattr(scheduler, "trigger_industry_now", lambda: ran.append("industry"))
    monkeypatch.setattr(pages, "require_csrf", lambda req, tok: None)
    monkeypatch.setattr(pages, "flash", lambda req, msg, *a: ran.append("flash"))

    class DB:
        pass
    monkeypatch.setattr(pages.sync_info, "pressed_message", lambda db, kind: kind)
    r = pages.sync_all(object(), "t", DB())
    assert ran == ["contracts", "industry", "flash"] and r.headers["location"] == "/system"
