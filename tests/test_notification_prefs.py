from datetime import timedelta

from sqlalchemy import select


def test_job_rule_match_only_watched_manufacturing():
    from app.services.prefs import job_rule_match
    rules = {"freighters", "capitals"}
    assert job_rule_match(1, "Some Freighter", "Freighter", rules, []) == "Freighters"
    assert job_rule_match(1, "Some JF", "Jump Freighter", rules, [])
    assert job_rule_match(1, "Some Dread", "Dreadnought", rules, []) == "All capital ships"
    assert job_rule_match(1, "Shuttle", "Shuttle", rules, []) is None          # not watched
    assert job_rule_match(9, "Fullerides", "Composite", rules, []) is None     # reaction
    assert job_rule_match(8, "Some Freighter", "Freighter", rules, []) is None  # invention
    assert job_rule_match(1, "Titan Hull", "Titan", {"freighters"}, []) is None  # not watched
    # The most specific rule names the alert: a titan is "Titans", not "All capital ships".
    assert job_rule_match(1, "Avatar", "Titan", {"capitals", "titans"}, []) == "Titans"
    assert job_rule_match(1, "Avatar", "Titan", {"capitals"}, []) == "All capital ships"
    assert job_rule_match(1, "Rorqual", "Capital Industrial Ship", {"rorquals"}, []) == "Rorquals"
    assert job_rule_match(1, "Rorqual Hull", "Other", set(), ["rorqual"]) == 'keyword "rorqual"'


def test_switched_off_kinds_are_not_queued(db):
    from app.models import Notification
    from app.services import prefs
    from app.services.notifier import enqueue

    assert prefs.is_enabled(db, "status_accepted")  # default on
    prefs.save(db, {"kind_new_contract": "1"})       # everything else unticked
    assert not enqueue(db, ref="c:1", kind="status_accepted", title="x")
    assert enqueue(db, ref="c:1", kind="new_contract", title="x")
    assert enqueue(db, ref="t:1", kind="test", title="x")  # test DM always allowed
    db.commit()
    assert {n.kind for n in db.scalars(select(Notification))} == {"new_contract", "test"}


def test_keywords_saved_and_parsed(db):
    from app.services import prefs
    prefs.save(db, {"kind_industry_job_ready": "1", "rule_freighters": "1",
                    "industry_keywords": "Rorqual,  Orca\nCapital Construction Parts"})
    rules, kws = prefs.industry_rules(db)
    assert rules == {"freighters"}
    assert kws == ["rorqual", "orca", "capital construction parts"]


def test_finished_freighter_job_alerts_once_but_shuttle_does_not(db):
    from app.db import utcnow
    from app.models import IndustryJob, Notification, TypeInfo
    from app.services.industry import queue_job_alerts

    now = utcnow()
    db.add(TypeInfo(type_id=100, name="Big Hauler", group_name="Freighter"))
    db.add(TypeInfo(type_id=200, name="Tiny Shuttle", group_name="Shuttle"))
    db.add(TypeInfo(type_id=300, name="Big Hauler Two", group_name="Freighter"))
    common = dict(owner_type="character", owner_id=1, installer_id=1, blueprint_type_id=1,
                  start_date=now - timedelta(days=2))
    db.add(IndustryJob(job_id=1, activity_id=1, product_type_id=100, status="active",
                       end_date=now - timedelta(minutes=1), **common))        # finished
    db.add(IndustryJob(job_id=2, activity_id=1, product_type_id=200, status="ready",
                       end_date=now - timedelta(hours=1), **common))          # shuttle
    db.add(IndustryJob(job_id=3, activity_id=1, product_type_id=300, status="active",
                       end_date=now + timedelta(hours=5), **common))          # still building
    db.commit()

    assert queue_job_alerts(db) == 1
    assert queue_job_alerts(db) == 0  # never twice for the same job
    db.commit()
    [n] = db.scalars(select(Notification)).all()
    assert n.kind == "industry_job_ready" and "Big Hauler" in n.title


def test_new_kinds_start_off_but_existing_ones_keep_working(db):
    from app.services import prefs
    assert not prefs.is_enabled(db, "research_job_ready")     # new: off until switched on
    assert prefs.is_enabled(db, "new_contract")               # existing default unchanged


def test_new_watch_groups_inherit_the_old_coarse_settings(db):
    """Upgrading changes nothing: until the page is saved, the split groups follow the
    old freighters / capitals / supercaps switches."""
    from app.models import Setting
    from app.services import prefs
    db.add(Setting(key="notify:industry:freighters", value="1"))
    db.add(Setting(key="notify:industry:capitals", value="0"))
    db.add(Setting(key="notify:industry:supercaps", value="0"))
    db.commit()
    prefs.invalidate()
    rules, _ = prefs.industry_rules(db)
    assert rules == {"freighters", "jump_freighters"}
    prefs.save(db, {"rule_titans": "1", "rule_rorquals": "1"})
    assert prefs.industry_rules(db)[0] == {"titans", "rorquals"}


def test_research_job_alerts_only_when_switched_on(db):
    from app.db import utcnow
    from app.models import EntityName, IndustryJob, Notification
    from app.services import prefs
    from app.services.industry import queue_job_alerts
    now = utcnow()
    db.add(EntityName(entity_id=500, name="Obelisk Blueprint", category="inventory_type"))
    db.add(IndustryJob(job_id=77, owner_type="character", owner_id=1, installer_id=1,
                       activity_id=4, blueprint_type_id=500, product_type_id=500, runs=10,
                       status="active", start_date=now - timedelta(days=1),
                       end_date=now - timedelta(minutes=1)))
    db.commit()
    assert queue_job_alerts(db) == 0                       # off by default
    prefs.save(db, {"kind_research_job_ready": "1"})
    assert queue_job_alerts(db) == 1
    assert queue_job_alerts(db) == 0                       # once per job
    db.commit()
    [n] = db.scalars(select(Notification)).all()
    assert n.kind == "research_job_ready" and n.title == "🔬 ME research finished: Obelisk Blueprint"


def test_settings_page_renders_groups_switches_and_keywords(db):
    from app.services import prefs
    from fastapi.testclient import TestClient
    from starlette.requests import Request
    from app.main import app
    prefs.save(db, {"industry_keywords": "Orca, Ragnarok"})

    async def login(request: Request):
        request.session["user"] = {"character_id": 1, "name": "x"}
        return {}
    app.add_api_route("/__test/login4", login)
    try:
        c = TestClient(app)
        c.get("/__test/login4")
        html = c.get("/settings/notifications").text
        for label in ("Freighters", "Jump Freighters", "Rorquals", "All capital ships",
                      "Supercarriers", "Titans", "Research job finished"):
            assert label in html
        assert 'name="kind_research_job_ready" value="1" >' in html   # unchecked
        assert '<span class="chip">orca</span>' in html
    finally:
        app.router.routes[:] = [r for r in app.router.routes
                                if getattr(r, "path", "") != "/__test/login4"]


def test_contract_updates_are_two_combined_switches(db):
    from app.services import prefs
    labels = [sw.label for sw in prefs.SWITCHES if sw.group == "Hauling"]
    assert labels == ["New courier received", "Unaccepted contract expiring soon",
                      "Delivery deadline approaching", "Contract accepted, delivered, or failed",
                      "Contract cancelled, deleted, or rejected", "Bulk job assigned",
                      "Bulk job contracts expiring soon"]
    # New kinds start off.
    assert not prefs.is_enabled(db, "bulk_job") and not prefs.is_enabled(db, "bulk_expiring")
    prefs.save(db, {"kind_status_progress": "1"})          # closed group left off
    for k in ("status_accepted", "status_finished", "status_failed"):
        assert prefs.is_enabled(db, k)
    for k in ("status_cancelled", "status_deleted", "status_rejected"):
        assert not prefs.is_enabled(db, k)
