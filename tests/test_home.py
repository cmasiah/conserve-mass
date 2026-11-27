from datetime import timedelta


def _contract(cid, status, issuer=500, **kw):
    from app.db import utcnow
    from app.models import Contract
    return Contract(contract_id=cid, corporation_id=98000001, issuer_id=issuer, type="courier",
                    status=status, date_issued=utcnow() - timedelta(hours=2), **kw)


def test_home_summary_counts(db):
    from app.db import utcnow
    from app.models import Customer, EsiToken, Character, IndustryJob, SyncState
    from app.services import home

    now = utcnow()
    db.add(Customer(character_id=500, name="Regular"))
    db.add(Customer(character_id=501, name="Blocked", blocked=True))
    db.add(Customer(character_id=502, name="Spammer", muted=True, flagged_at=now,
                    flag_reason="12 contracts in 1 hour"))
    db.add_all([
        _contract(1, "outstanding", date_expired=now + timedelta(hours=3)),
        _contract(2, "outstanding", date_expired=now + timedelta(days=5)),
        _contract(3, "outstanding", issuer=501),               # blocked: not counted
        _contract(4, "in_progress", collateral_isk=2e9),
        _contract(5, "finished", reward_isk=50e6, date_completed=now - timedelta(days=2)),
        _contract(6, "finished", reward_isk=99e6, date_completed=now - timedelta(days=40)),
    ])
    db.add(Character(character_id=7, name="Lapsed", corporation_id=98000001))
    db.add(EsiToken(character_id=7, refresh_token_enc=b"x", is_valid=False))
    db.add(SyncState(key="corp_contracts:98000001", last_error="502: bad gateway",
                     last_attempt_at=now))
    for jid, status, end in [(1, "active", now + timedelta(hours=2)),
                             (2, "active", now - timedelta(minutes=1)),   # done, not delivered
                             (3, "ready", now - timedelta(hours=1))]:
        db.add(IndustryJob(job_id=jid, owner_type="character", owner_id=7, installer_id=7,
                           activity_id=1, blueprint_type_id=1, status=status,
                           start_date=now - timedelta(hours=3), end_date=end))
    db.commit()

    d = home.build(db, bot_configured=True, bot_ready=True, update_available=True, latest="9.9.9")
    h = d["hauling"]
    assert (h.outstanding, h.in_transit, h.expiring_24h) == (2, 1, 1)
    assert h.collateral_at_risk == 2e9
    assert (h.delivered_30d, h.reward_30d) == (1, 50e6)
    i = d["industry"]
    assert (i.ready_jobs, i.active_jobs) == (2, 1)
    texts = " | ".join(a.text for a in d["attention"])
    assert "Spammer was auto-muted" in texts
    assert "Lapsed needs re-linking" in texts
    assert "502: bad gateway" in texts
    assert "9.9.9" in texts
    assert "Discord" not in texts


def test_home_empty_database(db):
    from app.services import home
    d = home.build(db, bot_configured=False, bot_ready=False, update_available=False, latest=None)
    assert d["hauling"].outstanding == 0 and d["industry"].characters == 0
    assert [a.text for a in d["attention"]] == ["Discord isn't configured; alerts are queuing"]


def test_home_widgets(db):
    from app.db import utcnow
    from app.models import (Character, CharacterSkill, Customer, EntityName,
                            IndustryJob)
    from app.services import home
    from app.services.industry import store_skill_queue

    now = utcnow()
    db.add(Customer(character_id=500, name="Big Spender"))
    db.add(Customer(character_id=501, name="Small Fry"))
    db.add_all([
        _contract(1, "finished", reward_isk=300e6, date_completed=now - timedelta(minutes=5)),
        _contract(2, "finished", issuer=501, reward_isk=10e6,
                  date_completed=now - timedelta(minutes=5)),
        _contract(3, "in_progress", date_accepted=now - timedelta(days=2), days_to_complete=5),
        _contract(4, "outstanding", date_expired=now + timedelta(hours=2)),
    ])
    for cid, nm in [(1, "Idle Ivy"), (2, "Busy Bob"), (3, "No Queue")]:
        db.add(Character(character_id=cid, name=nm, corporation_id=98000001))
    db.add(EntityName(entity_id=3387, name="Mass Production", category="inventory_type"))
    db.add(EntityName(entity_id=20185, name="Charon", category="inventory_type"))
    for cid in (1, 2):
        db.add(CharacterSkill(character_id=cid, skill_id=3387, trained_level=5, active_level=5))
    for jid in range(6):  # Bob: all 6 manufacturing slots busy
        db.add(IndustryJob(job_id=jid + 1, owner_type="character", owner_id=2, installer_id=2,
                           activity_id=1, blueprint_type_id=1, product_type_id=20185,
                           status="active", start_date=now - timedelta(hours=10),
                           end_date=now + timedelta(hours=jid + 1 if jid < 3 else 48)))
    db.flush()
    store_skill_queue(db, 1, [])
    store_skill_queue(db, 2, [
        {"skill_id": 3387, "queue_position": 0, "finished_level": 5,
         "finish_date": (now - timedelta(hours=1)).isoformat()},       # already done: ignored
        {"skill_id": 3387, "queue_position": 1, "finished_level": 5,
         "finish_date": (now + timedelta(days=3)).isoformat()}])
    db.commit()

    d = home.build(db, bot_configured=True, bot_ready=True, update_available=False, latest=None)
    assert d["isk_weeks"][-1]["isk"] == 310e6 and d["isk_weeks"][-1]["pct"] == 100
    assert len(d["isk_weeks"]) == 8
    assert [t["name"] for t in d["top_customers"]] == ["Big Spender", "Small Fry"]
    assert [(x["kind"], x["level"]) for x in d["deadlines"]] == [("accept", "bad"),
                                                                  ("deliver", "")]
    assert len(d["jobs_24h"]) == 3 and d["jobs_24h"][0]["what"] == "Charon"
    # Bob's 6 manufacturing slots are full but his 1 base science slot is free; idle first.
    assert [(r["character"].name, r["idle"]) for r in d["idle"]] == [("Idle Ivy", True),
                                                                      ("Busy Bob", False)]
    assert d["idle"][1]["free"]["manufacturing"] == 0
    states = {r["character"].name: r["state"] for r in d["queues"]}
    assert states == {"Idle Ivy": "Empty", "Busy Bob": "Training"}
