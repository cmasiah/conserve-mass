"""Bridge to the host-side updater (unraid/update-watcher.sh run by Unraid User Scripts).

The app never touches Docker. It only drops a request file in /config; the
watcher on the host sees it, runs update.sh (pull + restart) and writes the
result back. Files (all in the config dir):
  update-requested   created by the app, removed by the watcher
  updater-heartbeat  touched by the watcher every run (proves it's installed)
  update-status      key=value result of the last run
  update-last.log    output of the last run
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings
from app.db import utcnow

ACTIVE_WITHIN = timedelta(minutes=3)


def _dir() -> Path:
    return get_settings().config_dir


@dataclass
class UpdaterStatus:
    installed: bool
    last_seen: datetime | None
    pending: bool
    requested_at: datetime | None
    last_result: str | None
    last_started: str | None
    last_finished: str | None
    last_version: str | None
    log_tail: str


def _mtime(p: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def status() -> UpdaterStatus:
    d = _dir()
    hb = _mtime(d / "updater-heartbeat")
    kv: dict[str, str] = {}
    try:
        for line in (d / "update-status").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                kv[k.strip()] = v.strip()
    except OSError:
        pass
    try:
        log_tail = "\n".join((d / "update-last.log").read_text(errors="replace")
                             .splitlines()[-40:])
    except OSError:
        log_tail = ""
    return UpdaterStatus(
        installed=bool(hb and utcnow() - hb < ACTIVE_WITHIN),
        last_seen=hb,
        pending=(d / "update-requested").exists(),
        requested_at=_mtime(d / "update-requested"),
        last_result=kv.get("result"),
        last_started=kv.get("started"),
        last_finished=kv.get("finished"),
        last_version=kv.get("version"),
        log_tail=log_tail,
    )


def request_update(requested_by: str) -> None:
    (_dir() / "update-requested").write_text(
        f"requested_at={utcnow().isoformat()}\nby={requested_by}\n")
