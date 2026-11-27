"""Industry: slot capacity from skills, job tracking, blueprint library.

ESI budget (all calls honour each endpoint's Expires/ETag via sync_state):
  * skills       1 call per character per INDUSTRY_SKILLS_POLL_MINUTES
  * jobs         1 call per character + 1 per corp (paged) per INDUSTRY_JOBS_POLL_MINUTES
  * blueprints   1 call per character + 1 per corp (paged) per INDUSTRY_BLUEPRINTS_POLL_MINUTES
  * divisions    1 call per corp, with the blueprint sync
  * names        skill/type IDs resolved once in batches and cached forever
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import session_scope, utcnow
from app.esi import client as esi
from app.config import get_settings
from app.esi import resolver
from app.services import prefs
from app.services.notifier import enqueue
from app.services.type_info import resolve_types
from app.models import (
    Blueprint, Character, CharacterSkill, Corporation, EntityName, EsiToken, IndustryJob,
    Setting, SkillQueue, TypeInfo,
)

log = logging.getLogger(__name__)

SCOPE_SKILLS = "esi-skills.read_skills.v1"
SCOPE_SKILLQUEUE = "esi-skills.read_skillqueue.v1"
SCOPE_CHAR_JOBS = "esi-industry.read_character_jobs.v1"
SCOPE_CORP_JOBS = "esi-industry.read_corporation_jobs.v1"
SCOPE_CHAR_BPS = "esi-characters.read_blueprints.v1"
SCOPE_CORP_BPS = "esi-corporations.read_blueprints.v1"
SCOPE_DIVISIONS = "esi-corporations.read_divisions.v1"

# ---------------------------------------------------------------- slot math (pure)

# Each category: 1 base slot + level of the first skill + level of the advanced skill.
SLOT_SKILLS = {
    "manufacturing": ("Mass Production", "Advanced Mass Production"),
    "science": ("Laboratory Operation", "Advanced Laboratory Operation"),
    "reactions": ("Mass Reactions", "Advanced Mass Reactions"),
}
MAX_SLOTS = 11
REACTIONS_PREREQ = "Reactions"   # no reaction jobs at all without it
CAPITAL_SKILL = "Capital Ship Construction"
CAPITAL_TIERS = {
    0: "None",
    1: "Capital components",
    2: "Capital components",
    3: "Most capitals",
    4: "Jump freighters & supercarriers",
    5: "Titans",
}
# Remote-start skills: without them, jobs can only be started in your current system.
REMOTE_SKILLS = {
    "manufacturing": "Supply Chain Management",
    "science": "Scientific Networking",
    "reactions": "Remote Reactions",
}
# Verified range: +5 jumps per level, 25 at V. (Remote Reactions' range isn't stated
# here because we couldn't confirm CCP's exact numbers.)
REMOTE_JUMPS_PER_LEVEL = {"Supply Chain Management": 5, "Scientific Networking": 5}
CATEGORY_LABELS = {"manufacturing": "Manufacturing", "science": "Research",
                   "reactions": "Reactions"}
TRACKED_SKILLS = {s for pair in SLOT_SKILLS.values() for s in pair} | {
    REACTIONS_PREREQ, CAPITAL_SKILL, "Industry", "Advanced Industry"} | set(REMOTE_SKILLS.values())

# ESI activity_id -> slot category
ACTIVITY_CATEGORY = {1: "manufacturing", 3: "science", 4: "science", 5: "science",
                     7: "science", 8: "science", 9: "reactions", 11: "reactions"}
ACTIVITY_NAMES = {1: "Manufacturing", 3: "TE research", 4: "ME research", 5: "Copying",
                  7: "Reverse engineering", 8: "Invention", 9: "Reactions", 11: "Reactions"}


def slot_capacity(levels: dict[str, int]) -> dict[str, int]:
    """levels: skill name -> active level. Returns max slots per category."""
    out = {}
    for cat, (base, adv) in SLOT_SKILLS.items():
        out[cat] = 1 + levels.get(base, 0) + levels.get(adv, 0)
    if levels.get(REACTIONS_PREREQ, 0) == 0:
        out["reactions"] = 0
    return out


def slot_breakdown(levels: dict[str, int], cat: str) -> dict:
    """What a slot count is made of, for the hover on the Slots page."""
    base, adv = SLOT_SKILLS[cat]
    rows = []
    if cat == "reactions":
        lvl = levels.get(REACTIONS_PREREQ, 0)
        rows.append({"name": REACTIONS_PREREQ, "level": lvl,
                     "note": "required" if lvl else "required — not trained"})
    rows.append({"name": base, "level": levels.get(base, 0), "note": "+1 slot / level"})
    rows.append({"name": adv, "level": levels.get(adv, 0), "note": "+1 slot / level"})
    remote = REMOTE_SKILLS[cat]
    rl = levels.get(remote, 0)
    per = REMOTE_JUMPS_PER_LEVEL.get(remote)
    if rl == 0:
        reach = "current system only"
    elif per:
        reach = f"within {rl * per} jumps"
    else:
        reach = f"level {rl} remote range"
    return {"rows": rows, "remote": {"name": remote, "level": rl, "reach": reach},
            "capacity": slot_capacity(levels)[cat]}


def capital_tier(levels: dict[str, int]) -> tuple[int, str]:
    lvl = levels.get(CAPITAL_SKILL, 0)
    return lvl, CAPITAL_TIERS.get(lvl, "None")


@dataclass
class CharacterIndustry:
    character: Character
    has_skills: bool
    levels: dict[str, int] = field(default_factory=dict)
    capacity: dict[str, int] = field(default_factory=dict)
    used: dict[str, int] = field(default_factory=dict)
    capital_level: int = 0
    capital_label: str = "None"
    skills_updated: datetime | None = None

    def free(self, cat: str) -> int:
        return max(self.capacity.get(cat, 0) - self.used.get(cat, 0), 0)


def used_slots(jobs: list[IndustryJob]) -> dict[int, dict[str, int]]:
    """installer_id -> {category: active job count}. Only 'active' jobs hold a slot here."""
    out: dict[int, dict[str, int]] = {}
    for j in jobs:
        cat = ACTIVITY_CATEGORY.get(j.activity_id)
        if cat and j.status == "active":
            out.setdefault(j.installer_id, {}).setdefault(cat, 0)
            out[j.installer_id][cat] += 1
    return out


def character_overview(db: Session) -> list[CharacterIndustry]:
    chars = db.scalars(select(Character).order_by(Character.name)).all()
    name_by_id = {e.entity_id: e.name for e in db.scalars(
        select(EntityName).where(EntityName.name.in_(TRACKED_SKILLS),
                                 EntityName.category == "inventory_type"))}
    rows = db.scalars(select(CharacterSkill).where(
        CharacterSkill.skill_id.in_(name_by_id.keys()))).all()
    by_char: dict[int, list[CharacterSkill]] = {}
    for r in rows:
        by_char.setdefault(r.character_id, []).append(r)
    any_skill = {cid for (cid,) in db.execute(select(CharacterSkill.character_id).distinct())}
    used = used_slots(db.scalars(select(IndustryJob)).all())

    out = []
    for c in chars:
        skills = by_char.get(c.character_id, [])
        levels = {name_by_id[s.skill_id]: s.active_level for s in skills}
        lvl, label = capital_tier(levels)
        out.append(CharacterIndustry(
            character=c, has_skills=c.character_id in any_skill, levels=levels,
            capacity=slot_capacity(levels) if c.character_id in any_skill else {},
            used=used.get(c.character_id, {}), capital_level=lvl, capital_label=label,
            skills_updated=max((s.updated_at for s in skills), default=None)))
    return out


# ---------------------------------------------------------------- sync helpers

def _tokens_with(db: Session, scope: str) -> list[EsiToken]:
    return [t for t in db.scalars(select(EsiToken).where(EsiToken.is_valid.is_(True)))
            if scope in t.scope_set]


async def _corp_token(db: Session, corp_id: int, scope: str) -> str | None:
    """A token from a character in `corp_id` that has `scope` (poller first)."""
    corp = db.get(Corporation, corp_id)
    candidates = _tokens_with(db, scope)
    candidates.sort(key=lambda t: t.character_id != (corp.sync_character_id if corp else None))
    for t in candidates:
        if t.character.corporation_id == corp_id:
            try:
                return await esi.access_token(db, t.character_id)
            except esi.NoValidToken:
                continue
    return None


def _dt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


def _tracked_corp_ids(db: Session) -> list[int]:
    return [c.corporation_id for c in db.scalars(
        select(Corporation).where(Corporation.is_tracked.is_(True)))]


# ---------------------------------------------------------------- skills

async def sync_skills() -> None:
    with session_scope() as db:
        for tok in _tokens_with(db, SCOPE_SKILLS):
            cid = tok.character_id
            try:
                token = await esi.access_token(db, cid)
                res = await esi.get_cached_paged(db, f"skills:{cid}", f"/characters/{cid}/skills",
                                                 token=token, paged=False)
            except (esi.EsiError, esi.NoValidToken) as e:
                log.warning("Skill sync failed for %s: %s", cid, e)
                db.commit()
                continue
            if res.not_modified:
                continue
            now = utcnow()
            skills = res.data.get("skills", [])
            db.execute(delete(CharacterSkill).where(CharacterSkill.character_id == cid))
            for s in skills:
                db.add(CharacterSkill(character_id=cid, skill_id=s["skill_id"],
                                      trained_level=s.get("trained_skill_level", 0),
                                      active_level=s.get("active_skill_level", 0),
                                      updated_at=now))
            db.flush()
            # Skill names: one batched call the first time, cached forever after.
            await resolver.resolve_names(db, {s["skill_id"] for s in skills})
            db.commit()
            log.info("Skills synced for %s (%d skills)", tok.character.name, len(skills))

        for tok in _tokens_with(db, SCOPE_SKILLQUEUE):
            cid = tok.character_id
            try:
                token = await esi.access_token(db, cid)
                res = await esi.get_cached_paged(db, f"skillqueue:{cid}",
                                                 f"/characters/{cid}/skillqueue",
                                                 token=token, paged=False)
            except (esi.EsiError, esi.NoValidToken) as e:
                log.warning("Skill queue sync failed for %s: %s", cid, e)
                db.commit()
                continue
            if res.not_modified:
                continue
            store_skill_queue(db, cid, res.data or [])
            await resolver.resolve_names(db, {e["skill_id"] for e in (res.data or [])})
            db.commit()


def _parse_dt(v):
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def store_skill_queue(db: Session, cid: int, entries: list[dict]) -> SkillQueue:
    """Summarise ESI /skillqueue. Finished entries can linger until login, so skip them."""
    now = utcnow()
    live = sorted((e for e in entries
                   if not e.get("finish_date") or _parse_dt(e["finish_date"]) > now),
                  key=lambda e: e.get("queue_position", 0))
    finishes = [_parse_dt(e["finish_date"]) for e in live if e.get("finish_date")]
    cur = live[0] if live else None
    row = db.get(SkillQueue, cid) or SkillQueue(character_id=cid)
    row.length = len(live)
    row.current_skill_id = cur["skill_id"] if cur else None
    row.current_level = cur.get("finished_level") if cur else None
    row.current_end = _parse_dt(cur.get("finish_date")) if cur else None
    row.queue_end = max(finishes) if finishes and len(finishes) == len(live) else None
    row.updated_at = now
    db.merge(row)
    return row


# ---------------------------------------------------------------- jobs

def _replace_jobs(db: Session, owner_type: str, owner_id: int, rows: list[dict]) -> int:
    now = utcnow()
    db.execute(delete(IndustryJob).where(IndustryJob.owner_type == owner_type,
                                         IndustryJob.owner_id == owner_id))
    kept = 0
    for j in rows:
        if j.get("status") not in ("active", "ready", "paused"):
            continue
        db.merge(IndustryJob(
            job_id=j["job_id"], owner_type=owner_type, owner_id=owner_id,
            installer_id=j["installer_id"], activity_id=j["activity_id"],
            blueprint_type_id=j["blueprint_type_id"], product_type_id=j.get("product_type_id"),
            runs=j.get("runs", 1), status=j["status"],
            location_id=j.get("output_location_id") or j.get("location_id")
            or j.get("station_id"),
            facility_id=j.get("facility_id"), start_date=_dt(j["start_date"]),
            end_date=_dt(j["end_date"]), last_seen_at=now))
        kept += 1
    return kept


async def sync_jobs() -> None:
    with session_scope() as db:
        type_ids: set[int] = set()
        for tok in _tokens_with(db, SCOPE_CHAR_JOBS):
            cid = tok.character_id
            try:
                token = await esi.access_token(db, cid)
                res = await esi.get_cached_paged(
                    db, f"jobs:char:{cid}", f"/characters/{cid}/industry/jobs", token=token,
                    params={"include_completed": "false"}, paged=False)
            except (esi.EsiError, esi.NoValidToken) as e:
                log.warning("Job sync failed for %s: %s", cid, e)
                db.commit()
                continue
            if not res.not_modified:
                _replace_jobs(db, "character", cid, res.data)
                type_ids |= {j.get("product_type_id") or 0 for j in res.data}
                type_ids |= {j.get("blueprint_type_id") or 0 for j in res.data}

        for corp_id in _tracked_corp_ids(db):
            token = await _corp_token(db, corp_id, SCOPE_CORP_JOBS)
            if not token:
                continue
            try:
                res = await esi.get_cached_paged(
                    db, f"jobs:corp:{corp_id}", f"/corporations/{corp_id}/industry/jobs",
                    token=token, params={"include_completed": "false"})
            except esi.EsiError as e:
                # 403 = the character lacks the Factory_Manager corp role.
                log.warning("Corp job sync failed for %s: %s", corp_id, e)
                db.commit()
                continue
            if not res.not_modified:
                _replace_jobs(db, "corporation", corp_id, res.data)
                type_ids |= {j.get("product_type_id") or 0 for j in res.data}
                type_ids |= {j.get("blueprint_type_id") or 0 for j in res.data}

        installers = {j.installer_id for j in db.scalars(select(IndustryJob))}
        await resolver.resolve_names(db, type_ids | installers)
        # Group lookups only for what manufacturing jobs build (alert rules use the group).
        await resolve_types(db, {j.product_type_id for j in db.scalars(
            select(IndustryJob).where(IndustryJob.activity_id == 1))})
        queue_job_alerts(db)
        db.commit()


def queue_job_alerts(db: Session) -> int:
    """DM once per finished manufacturing job whose product matches your watch rules.
    DB-only (no ESI), so it's also run every minute to catch jobs finishing between syncs."""
    if not prefs.is_enabled(db, "industry_job_ready"):
        return 0
    rules, keywords = prefs.industry_rules(db)
    if not rules and not keywords:
        return 0
    now = utcnow()
    done = db.scalars(select(IndustryJob).where(
        IndustryJob.activity_id == 1,
        (IndustryJob.status == "ready") | (IndustryJob.end_date <= now))).all()
    names = {e.entity_id: e.name for e in db.scalars(select(EntityName).where(
        EntityName.entity_id.in_({j.installer_id for j in done})))}
    corps = {c.corporation_id: c for c in db.scalars(select(Corporation))}
    base = get_settings().base_url
    queued = 0
    for j in done:
        ti = db.get(TypeInfo, j.product_type_id) if j.product_type_id else None
        if ti is None:
            continue  # type not looked up yet; next run
        rule = prefs.job_rule_match(j.activity_id, ti.name, ti.group_name, rules, keywords)
        if not rule:
            continue
        who = names.get(j.installer_id, str(j.installer_id))
        owner = (f"[{corps[j.owner_id].ticker}] corp job" if j.owner_type == "corporation"
                 and j.owner_id in corps and corps[j.owner_id].ticker else
                 "corp job" if j.owner_type == "corporation" else "personal job")
        if enqueue(db, ref=f"job:{j.job_id}", kind="industry_job_ready",
                   title=f"🏭 {ti.name} ready for delivery",
                   body=(f"**{j.runs}× {ti.name}** ({ti.group_name or 'item'}) finished.\n"
                         f"**Installer:** {who} · {owner}\n"
                         f"**Watched because:** {rule}"),
                   url=f"{base}/industry/jobs", line=f"{j.runs}× {ti.name} · {who}"):
            queued += 1
    return queued


async def check_job_alerts() -> None:
    with session_scope() as db:
        queue_job_alerts(db)


# ---------------------------------------------------------------- blueprints

def _replace_blueprints(db: Session, owner_type: str, owner_id: int, rows: list[dict]) -> None:
    now = utcnow()
    db.execute(delete(Blueprint).where(Blueprint.owner_type == owner_type,
                                       Blueprint.owner_id == owner_id))
    for b in rows:
        db.merge(Blueprint(
            item_id=b["item_id"], owner_type=owner_type, owner_id=owner_id,
            type_id=b["type_id"], location_id=b["location_id"],
            location_flag=b.get("location_flag", ""), quantity=b.get("quantity", -1),
            material_efficiency=b.get("material_efficiency", 0),
            time_efficiency=b.get("time_efficiency", 0), runs=b.get("runs", -1),
            last_seen_at=now))


async def _sync_divisions(db: Session, corp_id: int) -> None:
    token = await _corp_token(db, corp_id, SCOPE_DIVISIONS)
    if not token:
        return
    try:
        res = await esi.get_cached_paged(db, f"divisions:{corp_id}",
                                         f"/corporations/{corp_id}/divisions",
                                         token=token, paged=False)
    except esi.EsiError as e:
        log.warning("Division names unavailable for %s (needs Director): %s", corp_id, e)
        return
    if res.not_modified:
        return
    for d in res.data.get("hangar", []):
        key = f"division:{corp_id}:{d['division']}"
        row = db.get(Setting, key) or Setting(key=key, value="")
        row.value = d.get("name") or f"Division {d['division']}"
        db.add(row)


def division_names(db: Session, corp_id: int) -> dict[str, str]:
    """location_flag (CorpSAG1..7) -> division name."""
    out = {}
    for n in range(1, 8):
        row = db.get(Setting, f"division:{corp_id}:{n}")
        out[f"CorpSAG{n}"] = row.value if row else f"Division {n}"
    return out


async def sync_blueprints() -> None:
    with session_scope() as db:
        type_ids: set[int] = set()
        for tok in _tokens_with(db, SCOPE_CHAR_BPS):
            cid = tok.character_id
            try:
                token = await esi.access_token(db, cid)
                res = await esi.get_cached_paged(db, f"bps:char:{cid}",
                                                 f"/characters/{cid}/blueprints", token=token)
            except (esi.EsiError, esi.NoValidToken) as e:
                log.warning("Blueprint sync failed for %s: %s", cid, e)
                db.commit()
                continue
            if not res.not_modified:
                _replace_blueprints(db, "character", cid, res.data)
                type_ids |= {b["type_id"] for b in res.data}

        for corp_id in _tracked_corp_ids(db):
            await _sync_divisions(db, corp_id)
            token = await _corp_token(db, corp_id, SCOPE_CORP_BPS)
            if not token:
                continue
            try:
                res = await esi.get_cached_paged(db, f"bps:corp:{corp_id}",
                                                 f"/corporations/{corp_id}/blueprints",
                                                 token=token)
            except esi.EsiError as e:
                # 403 = the character lacks the Director corp role.
                log.warning("Corp blueprint sync failed for %s: %s", corp_id, e)
                db.commit()
                continue
            if not res.not_modified:
                _replace_blueprints(db, "corporation", corp_id, res.data)
                type_ids |= {b["type_id"] for b in res.data}
                log.info("Corp blueprints synced: %d", len(res.data))

        await resolver.resolve_names(db, type_ids)
        db.commit()
