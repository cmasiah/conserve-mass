"""Per-character details for the Characters page tabs: implants, jump clones, attributes
(neural remaps), SP totals and the full skill queue.

Kept as one small JSON blob per character in the settings table (`charinfo:<id>`), since
it's display-only and replaced wholesale on every sync."""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.db import session_scope, utcnow
from app.esi import client as esi
from app.esi import resolver
from app.models import Character, CharacterSkill, EntityName, Location, Setting

log = logging.getLogger(__name__)

SCOPE_SKILLS = "esi-skills.read_skills.v1"
SCOPE_IMPLANTS = "esi-clones.read_implants.v1"
SCOPE_CLONES = "esi-clones.read_clones.v1"
ATTRS = ("intelligence", "memory", "perception", "willpower", "charisma")
INFOMORPH_SYNCHRONIZING = 33399     # -1h clone jump cooldown per level
CLONE_JUMP_COOLDOWN_H = 24
LOCATION_LOOKUPS = 10


def _key(cid: int) -> str:
    return f"charinfo:{cid}"


def get(db: Session, cid: int) -> dict:
    row = db.get(Setting, _key(cid))
    try:
        return json.loads(row.value) if row else {}
    except ValueError:
        return {}


def update(db: Session, cid: int, **parts) -> dict:
    data = get(db, cid)
    data.update(parts)
    row = db.get(Setting, _key(cid)) or Setting(key=_key(cid), value="{}")
    row.value = json.dumps(data, default=str)
    db.merge(row)
    return data


def drop(db: Session, cid: int) -> None:
    row = db.get(Setting, _key(cid))
    if row:
        db.delete(row)


def _dt(v):
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


# ------------------------------------------------------------------ sync

async def _fetch(db: Session, key: str, path: str, token: str):
    res = await esi.get_cached_paged(db, key, path, token=token, paged=False)
    return None if res.not_modified else res.data


async def sync_character_info() -> None:
    """Attributes for every skills-tracked character; implants and clones when the token
    has those scopes. ETag-cached, so an unchanged character costs one cheap 304 each."""
    with session_scope() as db:
        chars = [c for c in db.query(Character).all()
                 if c.track_skills and c.token and c.token.is_valid]
        for ch in chars:
            cid, scopes = ch.character_id, ch.token.scope_set
            try:
                token = await esi.access_token(db, cid)
                if SCOPE_SKILLS in scopes:
                    attrs = await _fetch(db, f"attributes:{cid}",
                                         f"/characters/{cid}/attributes", token)
                    if attrs is not None:
                        update(db, cid, attributes=attrs)
                if SCOPE_IMPLANTS in scopes:
                    imps = await _fetch(db, f"implants:{cid}",
                                        f"/characters/{cid}/implants", token)
                    if imps is not None:
                        await resolver.resolve_names(db, set(imps))
                        update(db, cid, implants=imps)
                if SCOPE_CLONES in scopes:
                    clones = await _fetch(db, f"clones:{cid}",
                                          f"/characters/{cid}/clones", token)
                    if clones is not None:
                        update(db, cid, clones=clones)
                        await _name_clone_locations(db, clones, token)
                        await resolver.resolve_names(
                            db, {i for jc in clones.get("jump_clones", [])
                                 for i in jc.get("implants", [])})
                db.commit()
            except (esi.EsiError, esi.NoValidToken) as e:
                log.warning("Implant/clone sync failed for %s: %s", ch.name, e)
                db.commit()


async def _name_clone_locations(db: Session, clones: dict, token: str) -> None:
    budget = resolver.LookupBudget(LOCATION_LOOKUPS)
    ids = [(clones.get("home_location") or {}).get("location_id")]
    ids += [jc.get("location_id") for jc in clones.get("jump_clones", [])]
    for i in dict.fromkeys(i for i in ids if i):
        await resolver.resolve_location(db, i, token, budget)


# ------------------------------------------------------------------ page rows

def _names(db: Session, ids) -> dict[int, str]:
    ids = set(ids)
    if not ids:
        return {}
    return {e.entity_id: e.name for e in db.query(EntityName)
            .filter(EntityName.entity_id.in_(ids))}


def _place(db: Session, location_id) -> str:
    if not location_id:
        return ""
    loc = db.get(Location, location_id)
    return loc.display if loc else f"Location {location_id}"


def implant_rows(db: Session, chars: list[Character], now: datetime | None = None) -> dict:
    now = now or utcnow()
    out = {}
    for ch in chars:
        info = get(db, ch.character_id)
        scopes = ch.token.scope_set if ch.token else set()
        imps = info.get("implants") or []
        names = _names(db, imps)
        clones = info.get("clones") or {}
        jcs = clones.get("jump_clones") or []
        jc_names = _names(db, {i for jc in jcs for i in jc.get("implants", [])})
        attrs = info.get("attributes") or {}
        # Clone jump cooldown: 24h, minus 1h per Infomorph Synchronizing level.
        sync_lvl = 0
        sk = db.get(CharacterSkill, (ch.character_id, INFOMORPH_SYNCHRONIZING))
        if sk:
            sync_lvl = sk.active_level or 0
        last_jump = _dt(clones.get("last_clone_jump_date"))
        jump_ready = (last_jump + timedelta(hours=CLONE_JUMP_COOLDOWN_H - sync_lvl)
                      if last_jump else None)
        next_remap = _dt(attrs.get("accrued_remap_cooldown_date"))
        out[ch.character_id] = {
            "has_implants_scope": SCOPE_IMPLANTS in scopes,
            "has_clones_scope": SCOPE_CLONES in scopes,
            "synced": bool(info),
            "implants": sorted((names.get(i, str(i)) for i in imps)),
            "attributes": {a: attrs.get(a) for a in ATTRS} if attrs else {},
            "bonus_remaps": attrs.get("bonus_remaps", 0) if attrs else None,
            "last_remap": _dt(attrs.get("last_remap_date")),
            "next_remap": next_remap,
            "remap_ready": bool(attrs) and (next_remap is None or next_remap <= now),
            "home": _place(db, (clones.get("home_location") or {}).get("location_id")),
            "jump_clones": [{"name": jc.get("name") or "",
                             "where": _place(db, jc.get("location_id")),
                             "implants": sorted(jc_names.get(i, str(i))
                                                for i in jc.get("implants", []))}
                            for jc in jcs],
            "jump_ready": jump_ready,
            "can_jump": bool(clones) and (jump_ready is None or jump_ready <= now),
        }
    return out


def skill_rows(db: Session, chars: list[Character]) -> dict:
    out = {}
    for ch in chars:
        info = get(db, ch.character_id)
        skills = db.query(CharacterSkill).filter(
            CharacterSkill.character_id == ch.character_id).all()
        out[ch.character_id] = {
            "total_sp": info.get("total_sp"),
            "unallocated_sp": info.get("unallocated_sp") or 0,
            "count": len(skills),
            "at_v": sum(1 for s in skills if s.trained_level == 5),
            "alpha_limited": sum(1 for s in skills if s.active_level < s.trained_level),
            "updated": max((s.updated_at for s in skills), default=None),
        }
    return out


def queue_rows(db: Session, chars: list[Character], now: datetime | None = None) -> dict:
    now = now or utcnow()
    out = {}
    for ch in chars:
        entries = [e for e in (get(db, ch.character_id).get("queue") or [])
                   if not e.get("finish") or _dt(e["finish"]) > now]
        names = _names(db, {e["skill_id"] for e in entries})
        rows = [{"name": names.get(e["skill_id"], f"Skill {e['skill_id']}"),
                 "level": e.get("level"), "start": _dt(e.get("start")),
                 "finish": _dt(e.get("finish"))} for e in entries]
        cur = rows[0] if rows else None
        pct = None
        if cur and cur["start"] and cur["finish"] and cur["finish"] > cur["start"]:
            pct = max(0.0, min(1.0, (now - cur["start"]) / (cur["finish"] - cur["start"])))
        paused = bool(rows) and not cur["finish"]
        ends = rows[-1]["finish"] if rows and all(r["finish"] for r in rows) else None
        out[ch.character_id] = {
            "entries": rows, "current": cur, "pct": pct, "paused": paused, "ends": ends,
            "state": ("empty" if not rows else "paused" if paused
                      else "short" if ends and ends - now < timedelta(hours=24) else "training"),
        }
    return out
