from datetime import timedelta

from sqlalchemy import select


def test_job_rule_match_only_watched_manufacturing():
    from app.services.prefs import job_rule_match
    rules = {"freighters", "capitals"}
    assert job_rule_match(1, "Some Freighter", "Freighter", rules, []) .startswith("Freighters")
    assert job_rule_match(1, "Some JF", "Jump Freighter", rules, [])
    assert job_rule_match(1, "Some Dread", "Dreadnought", rules, []).startswith("Capital")
    assert job_rule_match(1, "Shuttle", "Shuttle", rules, []) is None          # not watched
    assert job_rule_match(9, "Fullerides", "Composite", rules, []) is None     # reaction
    assert job_rule_match(8, "Some Freighter", "Freighter", rules, []) is None  # invention
    assert job_rule_match(1, "Titan Hull", "Titan", rules, []) is None         # supercaps off
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
