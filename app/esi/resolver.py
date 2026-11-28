"""Resolve EVE IDs to names, cached in the DB.

Abuse-safe by design:
  * names are looked up in batches (one call per 1000 IDs);
  * a failing batch is retried in chunks, then one by one, keeping errors near the minimum;
  * IDs that can't be resolved are remembered for 24h instead of retried every sync;
  * new location lookups are capped per sync via LookupBudget.
"""
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.esi import client as esi
from app.models import EntityName, EsiToken, Location

log = logging.getLogger(__name__)

STATION_ID_MAX = 100_000_000  # NPC stations are below this; Upwell structures are above.
UNKNOWN_RETRY = timedelta(hours=12)      # locations we couldn't name (e.g. no docking access)
UNRESOLVABLE_RETRY = timedelta(hours=24)  # IDs /universe/names rejected
UNRESOLVABLE = "unresolvable"


@dataclass
class LookupBudget:
    """Caps how many ESI lookups one sync may spend on new locations."""
    remaining: int

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


FALLBACK_CHUNK = 25


async def isolate_failures(batch: list[int],
                           lookup: Callable[[list[int]], Awaitable[list[dict]]],
                           chunk: int = FALLBACK_CHUNK) -> tuple[list[dict], list[int]]:
    """Resolve a batch. If ESI rejects it (one bad ID fails the whole call), retry in
    chunks of `chunk`, then one at a time only inside chunks that fail.

    Errors are what count toward CCP's error limit, so this keeps them close to the
    minimum (1 + failing chunks + bad IDs) while still using few calls.
    Returns (resolved rows, ids that failed on their own)."""
    if not batch:
        return [], []
    try:
        return await lookup(batch), []
    except esi.EsiError:
        if len(batch) == 1:
            return [], batch
    rows: list[dict] = []
    bad: list[int] = []
    pieces = [batch[i:i + chunk] for i in range(0, len(batch), chunk)] \
        if len(batch) > chunk else [[i] for i in batch]
    for piece in pieces:
        if len(piece) == 1:
            try:
                rows.extend(await lookup(piece))
            except esi.EsiError:
                bad.extend(piece)
        else:
            r, b = await isolate_failures(piece, lookup, chunk=1)
            rows.extend(r)
            bad.extend(b)
    return rows, bad


async def _names_lookup(batch: list[int]) -> list[dict]:
    return await esi.post("/universe/names", batch)


async def resolve_names(db: Session, ids: set[int]) -> dict[int, EntityName]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    known = {e.entity_id: e for e in
             db.scalars(select(EntityName).where(EntityName.entity_id.in_(ids)))}
    now = utcnow()
    missing = [i for i in ids
               if i not in known
               or (known[i].category == UNRESOLVABLE
                   and now - known[i].resolved_at > UNRESOLVABLE_RETRY)]

    for start in range(0, len(missing), 1000):
        rows, bad = await isolate_failures(missing[start:start + 1000], _names_lookup)
        for row in rows:
            ent = known.get(row["id"]) or db.get(EntityName, row["id"]) \
                or EntityName(entity_id=row["id"])
            ent.name, ent.category, ent.resolved_at = row["name"], row["category"], now
            db.add(ent)
            known[row["id"]] = ent
        for i in bad:
            log.info("ID %s can't be resolved; won't retry for 24h", i)
            ent = known.get(i) or db.get(EntityName, i) or EntityName(entity_id=i)
            ent.name, ent.category, ent.resolved_at = str(i), UNRESOLVABLE, now
            db.add(ent)
            known[i] = ent
    db.flush()
    return {k: v for k, v in known.items() if v.category != UNRESOLVABLE}


async def _system(system_id: int) -> tuple[str | None, float | None]:
    try:
        sysinfo = await esi.get(f"/universe/systems/{system_id}")
        return sysinfo.get("name"), sysinfo.get("security_status")
    except esi.EsiError:
        return None, None


SCOPE_STRUCTURES = "esi-universe.read_structures.v1"
MAX_STRUCTURE_TRIES = 4     # a 403 costs rate-limit tokens and counts as an ESI error


def structure_readers(db: Session, prefer: list[int] | tuple = ()) -> list[tuple[int, str]]:
    """Characters that can ask ESI for structure names, `prefer` (e.g. the items' owner)
    first. Structure access is per character, so one character's 403 says nothing about
    the others."""
    toks = [t for t in db.scalars(select(EsiToken).where(EsiToken.is_valid.is_(True)))
            if SCOPE_STRUCTURES in t.scope_set]
    order = {cid: i for i, cid in enumerate(prefer)}
    toks.sort(key=lambda t: order.get(t.character_id, len(order)))
    return [(t.character_id, t.character.name if t.character else str(t.character_id))
            for t in toks]


async def resolve_location(db: Session, location_id: int | None, token: str | None = None,
                           budget: LookupBudget | None = None, *,
                           prefer: list[int] | tuple = (), source: str = "") -> Location | None:
    """Name a station or structure. For structures, try `token` (if given), then the
    preferred characters, then the rest, until one of them has access."""
    if not location_id:
        return None
    loc = db.get(Location, location_id)
    if loc and loc.name:
        return loc
    if loc and loc.resolved_at and utcnow() - loc.resolved_at < UNKNOWN_RETRY:
        return loc  # recently failed; don't hammer ESI
    if budget is not None and not budget.take():
        return loc  # over this sync's budget; try again next sync

    loc = loc or Location(location_id=location_id)
    db.add(loc)
    loc.resolved_at = utcnow()
    where = f" ({source})" if source else ""
    try:
        if location_id < STATION_ID_MAX:
            info = await esi.get(f"/universe/stations/{location_id}")
            loc.kind, loc.name, loc.system_id = "station", info["name"], info["system_id"]
        else:
            loc.kind = "structure"
            denied: list[str] = []
            attempts: list[tuple[str, str | None, int | None]] = []
            if token:
                attempts.append(("the sync's character", token, None))
            attempts += [(name, None, cid) for cid, name in structure_readers(db, prefer)]
            info = None
            for name, tok, cid in attempts[:MAX_STRUCTURE_TRIES]:
                if tok is None:
                    try:
                        tok = await esi.access_token(db, cid)
                    except esi.NoValidToken:
                        continue
                try:
                    info = await esi.get(f"/universe/structures/{location_id}", token=tok)
                    break
                except esi.EsiError as e:
                    if e.status != 403:
                        raise
                    denied.append(name)
            if info:
                loc.name, loc.system_id = info["name"], info["solar_system_id"]
            else:
                log.info("Structure %s%s: no access for %s; retrying in 24h", location_id,
                         where, ", ".join(dict.fromkeys(denied)) or "any character "
                         "(none has the structures scope)")
        if loc.system_id:
            loc.system_name, loc.security = await _system(loc.system_id)
    except esi.EsiError as e:
        log.info("Location %s%s unresolved: %s", location_id, where, e)
    db.flush()
    return loc
