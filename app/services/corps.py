"""Which corporations the app reads corp-level data for.

* The home corp (Conserve Mass) is the only one whose courier contracts are polled.
* Any linked character's corp is listed, but its corp jobs, blueprints and hangars are
  synced only when switched on (Characters page), and only through Director+ characters.
  Members' personal data never depends on this.
"""
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AssetNode, Blueprint, Corporation, IndustryJob, SyncState


def home_corp(db: Session) -> Corporation | None:
    return db.scalar(select(Corporation).where(Corporation.is_home.is_(True)).limit(1))


def corp_sync_keys(corp_id: int) -> list[str]:
    return [f"jobs:corp:{corp_id}", f"bps:corp:{corp_id}", f"divisions:{corp_id}",
            f"assets:corp:{corp_id}"]


def set_tracked(db: Session, corp: Corporation, on: bool) -> None:
    """Switch corp data on/off. Off deletes the corp jobs, blueprints and hangar places
    already pulled in (and their ESI cache markers, so switching back on refetches)."""
    corp.is_tracked = on
    if on:
        return
    cid = corp.corporation_id
    for model in (IndustryJob, Blueprint, AssetNode):
        db.execute(delete(model).where(model.owner_type == "corporation",
                                       model.owner_id == cid))
    db.execute(delete(SyncState).where(SyncState.key.in_(corp_sync_keys(cid))))


def ensure_corp(db: Session, corp_id: int, info: dict | None) -> tuple[Corporation, bool]:
    """Make sure a linked character's corp is listed. Returns (corp, is_new).
    The first corp (or CORPORATION_ID) becomes home and is tracked; others start off."""
    corp = db.get(Corporation, corp_id)
    is_new = corp is None
    if is_new:
        corp = Corporation(corporation_id=corp_id, is_tracked=False, is_home=False)
        db.add(corp)
    if info and not corp.name:
        corp.name, corp.ticker = info.get("name"), info.get("ticker")
    wanted = get_settings().corporation_id
    if not corp.is_home and home_corp(db) is None and (not wanted or wanted == corp_id):
        corp.is_home = True
        corp.is_tracked = True
    return corp, is_new
