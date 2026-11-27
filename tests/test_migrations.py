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
        == "0008_blueprint_places"


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
