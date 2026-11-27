"""Nightly online SQLite backup into /config/backups (safe while the app is running)."""
import logging
import sqlite3
from datetime import datetime, timedelta

from app.config import get_settings

log = logging.getLogger(__name__)


def run_backup() -> str:
    s = get_settings()
    s.backup_dir.mkdir(parents=True, exist_ok=True)
    src_path = s.config_dir / "conserve-mass.db"
    dest_path = s.backup_dir / f"conserve-mass-{datetime.now():%Y%m%d-%H%M%S}.db"

    src = sqlite3.connect(src_path)
    dest = sqlite3.connect(dest_path)
    try:
        with dest:
            src.backup(dest)
    finally:
        dest.close()
        src.close()

    cutoff = datetime.now() - timedelta(days=s.backup_keep_days)
    for old in s.backup_dir.glob("conserve-mass-*.db"):
        if datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
            old.unlink(missing_ok=True)
    log.info("Database backed up to %s", dest_path)
    return str(dest_path)
