"""Per-character tracking choices (Characters page toggles) and the scopes they need."""
from dataclasses import dataclass

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.esi.sso import PERSONAL_SCOPES
from app.models import Blueprint, Character, IndustryJob, SyncState

SKILLS = "esi-skills.read_skills.v1"
SKILLQUEUE = "esi-skills.read_skillqueue.v1"
CHAR_JOBS = "esi-industry.read_character_jobs.v1"
CHAR_BPS = "esi-characters.read_blueprints.v1"
ASSETS = "esi-assets.read_assets.v1"


@dataclass(frozen=True)
class Toggle:
    key: str          # matches Character.track_<key>
    label: str
    help: str
    scopes: tuple[str, ...]


TOGGLES = (
    Toggle("skills", "Skills",
           "Sync skill levels and the training queue. Slots are worked out from skills, "
           "and the Home page shows the queue.", (SKILLS, SKILLQUEUE)),
    Toggle("slots", "Slots",
           "Count this character on the Slots page and in the Home slot widgets "
           "(free slots, idle characters). Turn off for alts that never build.", ()),
    Toggle("industry", "Industry",
           "Sync this character's own industry jobs and blueprints. Corp jobs and "
           "blueprints aren't affected. Turning it off clears its personal ones.",
           (CHAR_JOBS, CHAR_BPS)),
    Toggle("assets", "Assets",
           "Include this character's own assets. Asset tracking isn't built yet; this "
           "choice applies once it is.", (ASSETS,)),
)
BY_KEY = {t.key: t for t in TOGGLES}


def is_on(char: Character, key: str) -> bool:
    return bool(getattr(char, f"track_{key}"))


def missing_scopes(char: Character) -> list[str]:
    """Personal scopes this character's enabled toggles need but its token lacks."""
    have = char.token.scope_set if char.token else set()
    need = [s for t in TOGGLES if is_on(char, t.key) for s in t.scopes]
    return [s for s in dict.fromkeys(need) if s not in have]


def has_personal_access(char: Character) -> bool:
    tok = char.token
    return bool(tok and tok.is_valid and set(PERSONAL_SCOPES) <= tok.scope_set)


def set_tracking(db: Session, char: Character, key: str, on: bool) -> None:
    """Flip one toggle. Switching industry off drops the character's personal jobs and
    blueprints (and their ESI cache markers, so switching back on refetches them)."""
    if key not in BY_KEY:
        raise ValueError(key)
    setattr(char, f"track_{key}", on)
    cid = char.character_id
    if key == "industry" and not on:
        db.execute(delete(IndustryJob).where(IndustryJob.owner_type == "character",
                                             IndustryJob.owner_id == cid))
        db.execute(delete(Blueprint).where(Blueprint.owner_type == "character",
                                           Blueprint.owner_id == cid))
        db.execute(delete(SyncState).where(SyncState.key.in_(
            [f"jobs:char:{cid}", f"bps:char:{cid}"])))
    if key == "skills" and not on:
        # Keep the last known skills (slots still work); just refetch fresh when re-enabled.
        db.execute(delete(SyncState).where(SyncState.key.in_(
            [f"skills:{cid}", f"skillqueue:{cid}"])))


def remove(db: Session, char: Character) -> None:
    """Delete a character and everything synced for it. Its token, skills and skill queue
    go by foreign-key cascade; personal jobs and blueprints are keyed by owner, so they're
    cleared here (same as switching Industry off). Corp data stays."""
    for key in ("industry", "skills"):
        set_tracking(db, char, key, False)
    db.delete(char)
