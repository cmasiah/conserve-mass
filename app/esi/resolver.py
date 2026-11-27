"""Resolve EVE IDs to names, cached in the DB."""
import logging
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.esi import client as esi
from app.models import EntityName, Location

log = logging.getLogger(__name__)

STATION_ID_MAX = 100_000_000  # NPC stations are below this; Upwell structures are above.
UNKNOWN_RETRY = timedelta(hours=12)


async def resolve_names(db: Session, ids: set[int]) -> dict[int, EntityName]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    cached = {e.entity_id: e for e in
              db.scalars(select(EntityName).where(EntityName.entity_id.in_(ids)))}
    missing = list(ids - cached.keys())

    async def _lookup(batch: list[int]) -> list[dict]:
        try:
            return await esi.post("/universe/names", batch)
        except esi.EsiError as e:
            # One invalid ID fails the whole batch; fall back to one at a time.
            if len(batch) == 1:
                log.info("Could not resolve id %s: %s", batch[0], e)
                return []
            out = []
            for i in batch:
                out.extend(await _lookup([i]))
            return out

    for start in range(0, len(missing), 1000):
        for row in await _lookup(missing[start:start + 1000]):
            ent = db.get(EntityName, row["id"]) or EntityName(entity_id=row["id"])
            ent.name, ent.category, ent.resolved_at = row["name"], row["category"], utcnow()
            db.add(ent)
            cached[row["id"]] = ent
    db.flush()
    return cached


async def _system(system_id: int) -> tuple[str | None, float | None]:
    try:
        sysinfo = await esi.get(f"/universe/systems/{system_id}")
        return sysinfo.get("name"), sysinfo.get("security_status")
    except esi.EsiError:
        return None, None


async def resolve_location(db: Session, location_id: int | None,
                           token: str | None = None) -> Location | None:
    if not location_id:
        return None
    loc = db.get(Location, location_id)
    if loc and loc.name:
        return loc
    if loc and loc.resolved_at and utcnow() - loc.resolved_at < UNKNOWN_RETRY:
        return loc  # recently failed; don't hammer ESI

    loc = loc or Location(location_id=location_id)
    db.add(loc)
    loc.resolved_at = utcnow()
    try:
        if location_id < STATION_ID_MAX:
            info = await esi.get(f"/universe/stations/{location_id}")
            loc.kind, loc.name, loc.system_id = "station", info["name"], info["system_id"]
        else:
            loc.kind = "structure"
            if token:
                info = await esi.get(f"/universe/structures/{location_id}", token=token)
                loc.name, loc.system_id = info["name"], info["solar_system_id"]
        if loc.system_id:
            loc.system_name, loc.security = await _system(loc.system_id)
    except esi.EsiError as e:
        # 403 = no docking access to that structure. Keep the row, retry later.
        log.info("Location %s unresolved: %s", location_id, e)
    db.flush()
    return loc
