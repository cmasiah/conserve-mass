from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def test_slot_capacity_matches_skill_rules():
    from app.services.industry import slot_capacity
    maxed = {"Mass Production": 5, "Advanced Mass Production": 5,
             "Laboratory Operation": 5, "Advanced Laboratory Operation": 5,
             "Reactions": 1, "Mass Reactions": 5, "Advanced Mass Reactions": 5}
    assert slot_capacity(maxed) == {"manufacturing": 11, "science": 11, "reactions": 11}
    assert slot_capacity({"Mass Production": 5})["manufacturing"] == 6
    assert slot_capacity({"Mass Production": 5, "Advanced Mass Production": 4}
                         )["manufacturing"] == 10
    assert slot_capacity({})["manufacturing"] == 1          # everyone gets one slot
    # No Reactions skill: can't run reaction jobs at all, whatever else is trained
    assert slot_capacity({"Mass Reactions": 5, "Advanced Mass Reactions": 5})["reactions"] == 0


def test_capital_tiers():
    from app.services.industry import capital_tier
    assert capital_tier({}) == (0, "None")
    assert capital_tier({"Capital Ship Construction": 1}) == (1, "Components only")
    assert capital_tier({"Capital Ship Construction": 3}) == (3, "Capital Hulls")
    assert capital_tier({"Capital Ship Construction": 4})[1] == "JF & Supers"
    assert capital_tier({"Capital Ship Construction": 5})[1] == "Titans"


def test_used_slots_counts_active_jobs_by_category():
    from app.services.industry import used_slots
    J = lambda who, act, status="active": SimpleNamespace(installer_id=who, activity_id=act,
                                                           status=status)
    used = used_slots([J(1, 1), J(1, 1), J(1, 8), J(1, 4), J(1, 11), J(1, 1, "ready"), J(2, 9)])
    assert used[1] == {"manufacturing": 2, "science": 2, "reactions": 1}
    assert used[2] == {"reactions": 1}


def test_character_overview_reads_skills_and_jobs(db):
    from app.db import utcnow
    from app.models import Character, CharacterSkill, EntityName, IndustryJob
    from app.services.industry import character_overview

    db.add(Character(character_id=1, name="Builder", corporation_id=98000001))
    db.add(Character(character_id=2, name="Fresh Alt", corporation_id=98000001))
    names = {3387: "Mass Production", 24625: "Advanced Mass Production",
             22242: "Capital Ship Construction"}
    for sid, nm in names.items():
        db.add(EntityName(entity_id=sid, name=nm, category="inventory_type"))
        db.add(CharacterSkill(character_id=1, skill_id=sid, trained_level=5,
                              active_level=4 if sid == 22242 else 5))
    now = utcnow()
    db.add(IndustryJob(job_id=1, owner_type="corporation", owner_id=98000001, installer_id=1,
                       activity_id=1, blueprint_type_id=1, status="active",
                       start_date=now - timedelta(hours=1), end_date=now + timedelta(hours=1)))
    db.commit()

    rows = {r.character.name: r for r in character_overview(db)}
    b = rows["Builder"]
    assert b.capacity["manufacturing"] == 11 and b.used["manufacturing"] == 1
    assert b.free("manufacturing") == 10
    assert b.capital_level == 4 and b.capital_label == "JF & Supers"
    assert not rows["Fresh Alt"].has_skills  # no data yet, shown as such rather than as 1 slot


def test_identical_blueprints_merge_into_one_line_with_count():
    from app.web.routes.industry import aggregate_blueprints
    B = lambda t, q=-2, me=10, te=20, runs=5: SimpleNamespace(
        type_id=t, quantity=q, material_efficiency=me, time_efficiency=te, runs=runs,
        is_copy=(q == -2))
    items = [{"b": B(1), "name": "Charon BPC"} for _ in range(40)]
    items += [{"b": B(1, runs=1), "name": "Charon BPC"}]                 # different runs
    items += [{"b": B(2, q=-1, runs=-1), "name": "Rifter BP"},
              {"b": B(2, q=3, runs=-1), "name": "Rifter BP"}]            # stack of 3 BPOs
    lines = aggregate_blueprints(items)
    assert sorted((x["name"], x["count"]) for x in lines) == [
        ("Charon BPC", 1), ("Charon BPC", 40), ("Rifter BP", 4)]
    assert lines[0]["count"] == 40                      # most first by default


def test_reaction_formulas_are_separated_from_manufacturing():
    from app.web.routes.industry import blueprint_kind
    assert blueprint_kind("Fullerides Reaction Formula") == "reactions"
    assert blueprint_kind("Obelisk Blueprint") == "manufacturing"


def test_identical_jobs_group_into_one_line():
    from app.web.routes.industry import group_jobs, job_icon
    now = datetime.now(timezone.utc)
    J = lambda jid, end_h, act=1, prod=20187: SimpleNamespace(
        job_id=jid, activity_id=act, product_type_id=prod, blueprint_type_id=20188, runs=1,
        end_date=now + timedelta(hours=end_h))
    view = [{"j": J(i, i), "pct": 50, "done": False, "what": "Obelisk", "icon": "x",
             "who": "Builder", "activity": "Manufacturing", "owner": "CMASS"} for i in (3, 1, 2, 4)]
    view.append({"j": J(9, 5, act=5, prod=20188), "pct": 10, "done": False, "what": "Obelisk BP",
                 "icon": "y", "who": "Lab", "activity": "Copying", "owner": "Personal"})
    groups = group_jobs(view)
    ob = next(g for g in groups if g["what"] == "Obelisk")
    assert ob["count"] == 4 and ob["runs"] == 4
    assert [v["j"].job_id for v in ob["jobs"]] == [1, 2, 3, 4]     # soonest first
    assert len(groups) == 2
    # Research/copy/invention produce blueprints: use the blueprint picture, not the item icon
    assert job_icon(5, 20188, 20188) == "types/20188/bpc"
    assert job_icon(4, 20188, 20188) == "types/20188/bp"
    assert job_icon(1, 20187, 20188) == "types/20187/icon"


def test_slot_breakdown_lists_skills_and_remote_range():
    from app.services.industry import slot_breakdown
    lv = {"Reactions": 4, "Mass Reactions": 5, "Advanced Mass Reactions": 4, "Remote Reactions": 2,
          "Mass Production": 4, "Advanced Mass Production": 2}
    r = slot_breakdown(lv, "reactions")
    assert r["capacity"] == 10
    assert [x["name"] for x in r["rows"]] == ["Reactions", "Mass Reactions",
                                             "Advanced Mass Reactions"]
    assert r["remote"]["name"] == "Remote Reactions" and r["remote"]["reach"] == "within 10 jumps"
    m = slot_breakdown(lv, "manufacturing")
    assert m["capacity"] == 7                                   # this character's max, not 11
    assert m["remote"]["reach"] == "current system only"        # no Supply Chain Management
    assert slot_breakdown({"Scientific Networking": 3}, "science")["remote"]["reach"] \
        == "within 15 jumps"
    assert slot_breakdown({}, "reactions")["capacity"] == 0     # no Reactions skill
    # Queued levels are reported separately from trained ones (white vs blue pips)
    q = slot_breakdown({"Mass Production": 3}, "manufacturing", {"Mass Production": 5})
    assert (q["rows"][0]["level"], q["rows"][0]["queued"]) == (3, 5)


def test_slots_filters_sorting_and_highlights(db):
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from app.main import app
    from app.models import Character, CharacterSkill, EntityName
    skills = {"Industry": 3380, "Mass Production": 3387, "Advanced Mass Production": 24625,
              "Capital Ship Construction": 22242}
    for n, i in skills.items():
        db.add(EntityName(entity_id=i, name=n, category="inventory_type"))
    for cid, name, mp, cap in [(1, "Alpha", 5, 5), (2, "Bravo", 2, 1), (3, "Charlie", 5, 3)]:
        db.add(Character(character_id=cid, name=name))
        for n, i in skills.items():
            lvl = {"Mass Production": mp, "Advanced Mass Production": mp,
                   "Capital Ship Construction": cap}.get(n, 5)
            db.add(CharacterSkill(character_id=cid, skill_id=i, trained_level=lvl,
                                  active_level=lvl))
    db.commit()

    async def login(request: Request):
        request.session["user"] = {"character_id": 1, "name": "Alpha"}
        return {}
    app.add_api_route("/__test/login3", login)
    try:
        c = TestClient(app)
        c.get("/__test/login3")
        html = c.get("/industry/slots?only=under:manufacturing").text
        assert "Bravo" in html and "Alpha" not in html.split("<tbody>")[1]
        assert "slotcell short" in html
        html = c.get("/industry/slots?only=gold:manufacturing").text
        body = html.split("<tbody>")[1]
        assert "Alpha" in body and "Charlie" in body and "Bravo" not in body
        assert "slotcell gold" not in html and "short of" not in html   # plain 10+
        body = c.get("/industry/slots?sort=capital&dir=desc").text.split("<tbody>")[1]
        assert body.index("Alpha") < body.index("Charlie") < body.index("Bravo")
        body = c.get("/industry/slots?sort=capital&dir=asc").text.split("<tbody>")[1]
        assert body.index("Bravo") < body.index("Charlie") < body.index("Alpha")
        body = c.get("/industry/slots?sort=name&dir=desc").text.split("<tbody>")[1]
        assert body.index("Charlie") < body.index("Bravo") < body.index("Alpha")
        assert 'class="tier tier-5"' in body and "Level 1–2" in body
        assert "Components only" in body
        jobs = c.get("/industry/jobs?owner=corporation").text
        assert '<option value="corporation" selected>' in jobs
    finally:
        app.router.routes[:] = [r for r in app.router.routes
                                if getattr(r, "path", "") != "/__test/login3"]
