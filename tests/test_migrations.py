"""Run the real Alembic migrations against throwaway SQLite files."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def alembic(config_dir: Path, *args: str) -> None:
    env = dict(os.environ, CONFIG_DIR=str(config_dir))
    res = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert res.returncode == 0, f"alembic {' '.join(args)} failed:\n{res.stderr[-3000:]}"


def test_fresh_database_upgrades_to_head(tmp_path):
    alembic(tmp_path, "upgrade", "head")
    db = sqlite3.connect(tmp_path / "conserve-mass.db")
    cols = {r[1] for r in db.execute("PRAGMA table_info(customers)")}
    assert {"muted", "blocked", "flag_reason", "flagged_at"} <= cols
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"character_skills", "industry_jobs", "blueprints", "type_info",
            "skill_queues"} <= tables
    assert "queued" in {r[1] for r in db.execute("PRAGMA table_info(skill_queues)")}


def test_0002_survives_linked_contracts_and_failed_0_4_0_leftovers(tmp_path):
    """Reproduces the 0.4.0 crash: customers referenced by contracts, plus the
    _alembic_tmp_customers table the failed batch migration left behind."""
    alembic(tmp_path, "upgrade", "0001_initial")
    db = sqlite3.connect(tmp_path / "conserve-mass.db")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("INSERT INTO corporations (corporation_id, is_tracked) VALUES (1, 1)")
    db.execute("INSERT INTO customers (character_id, name, first_seen_at, tag) "
               "VALUES (9, 'Bob', '2026-01-01 00:00:00', 'blocked')")
    db.execute("INSERT INTO contracts (contract_id, corporation_id, issuer_id, type, status, "
               "date_issued, work_status, first_seen_at, last_seen_at) VALUES "
               "(100, 1, 9, 'courier', 'finished', '2026-01-01 00:00:00', 'delivered', "
               "'2026-01-01 00:00:00', '2026-01-01 00:00:00')")
    db.execute("CREATE TABLE _alembic_tmp_customers (character_id BIGINT)")
    db.commit()
    db.close()

    alembic(tmp_path, "upgrade", "head")
    alembic(tmp_path, "upgrade", "head")  # re-running is a no-op

    db = sqlite3.connect(tmp_path / "conserve-mass.db")
    cols = {r[1] for r in db.execute("PRAGMA table_info(customers)")}
    assert {"muted", "blocked", "flag_reason", "flagged_at"} <= cols
    ncols = {r[1] for r in db.execute("PRAGMA table_info(notifications)")}
    assert {"line", "subject_id"} <= ncols
    assert db.execute("SELECT name, blocked, muted FROM customers").fetchall() == [("Bob", 1, 0)]
    assert db.execute("SELECT count(*) FROM contracts").fetchone()[0] == 1
    assert db.execute("SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp%'"
                      ).fetchall() == []
    assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] \
        == "0009_corp_tracking_roles"


def test_0007_turns_tracking_on_for_existing_characters(tmp_path):
    alembic(tmp_path, "upgrade", "0006_skill_queue_levels")
    db = sqlite3.connect(tmp_path / "conserve-mass.db")
    db.execute("INSERT INTO characters (character_id, name, is_admin, created_at, updated_at) "
               "VALUES (1, 'Old Pilot', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')")
    db.commit()
    db.close()
    alembic(tmp_path, "upgrade", "head")
    alembic(tmp_path, "upgrade", "head")  # idempotent
    db = sqlite3.connect(tmp_path / "conserve-mass.db")
    row = db.execute("SELECT track_skills, track_slots, track_industry, track_assets "
                     "FROM characters").fetchone()
    assert row == (1, 1, 1, 1)


def test_0009_keeps_only_the_home_corp_tracked_and_clears_the_others(tmp_path):
    """The live situation: linking characters tracked their corps too (Conserve Mass plus
    two others), pulling in those corps' blueprints and jobs."""
    alembic(tmp_path, "upgrade", "0008_blueprint_places")
    db = sqlite3.connect(tmp_path / "conserve-mass.db")
    ts = "'2026-01-01 00:00:00'"
    chars = [(1, "Gavin", 100, 1, "esi-contracts.read_corporation_contracts.v1"),
             (2, "Aldois", 200, 0, "esi-corporations.read_blueprints.v1 esi-skills.read_skills.v1"),
             (3, "Kashi", 300, 0, "esi-industry.read_corporation_jobs.v1"),
             (4, "Alt", 100, 0, "esi-skills.read_skills.v1")]
    for cid, name, corp, admin, scopes in chars:
        db.execute(f"INSERT INTO characters (character_id, name, corporation_id, is_admin, "
                   f"created_at, updated_at) VALUES ({cid}, '{name}', {corp}, {admin}, {ts}, {ts})")
        db.execute(f"INSERT INTO esi_tokens (character_id, refresh_token_enc, scopes, is_valid, "
                   f"updated_at) VALUES ({cid}, x'00', '{scopes}', 1, {ts})")
    for corp, name, poller in [(100, "Conserve Mass", 1), (200, "Cherry", 2), (300, "Soul", 3)]:
        db.execute(f"INSERT INTO corporations (corporation_id, name, is_tracked, "
                   f"sync_character_id) VALUES ({corp}, '{name}', 1, {poller})")
    for item, otype, owner in [(1, "corporation", 100), (2, "corporation", 200),
                               (3, "character", 2)]:
        db.execute(f"INSERT INTO blueprints (item_id, owner_type, owner_id, type_id, location_id, "
                   f"location_flag, quantity, material_efficiency, time_efficiency, runs, "
                   f"last_seen_at) VALUES ({item}, '{otype}', {owner}, 1, 1, 'X', -1, 0, 0, -1, {ts})")
    db.execute("INSERT INTO sync_state (key) VALUES ('bps:corp:200')")
    db.commit()
    db.close()

    alembic(tmp_path, "upgrade", "head")
    alembic(tmp_path, "upgrade", "head")
    db = sqlite3.connect(tmp_path / "conserve-mass.db")
    assert db.execute("SELECT corporation_id, is_tracked, is_home, sync_character_id "
                      "FROM corporations ORDER BY 1").fetchall() == [
        (100, 1, 1, 1), (200, 0, 0, None), (300, 0, 0, None)]
    # Cherry's corp blueprints are gone; Conserve Mass's and Aldois's personal ones stay.
    assert db.execute("SELECT item_id FROM blueprints ORDER BY 1").fetchall() == [(1,), (3,)]
    assert db.execute("SELECT count(*) FROM sync_state WHERE key='bps:corp:200'").fetchone() == (0,)
    roles = dict(db.execute("SELECT name, link_role FROM characters").fetchall())
    assert roles == {"Gavin": "member", "Aldois": "director", "Kashi": "director",
                     "Alt": "member"}
