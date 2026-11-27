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
    assert capital_tier({"Capital Ship Construction": 1}) == (1, "Capital components")
    assert capital_tier({"Capital Ship Construction": 3}) == (3, "Most capitals")
    assert capital_tier({"Capital Ship Construction": 4})[1] == "Jump freighters & supercarriers"
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
    assert b.capital_level == 4 and "Jump freighters" in b.capital_label
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
