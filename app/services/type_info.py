"""Item type -> group lookup via public ESI endpoints, cached permanently in type_info.

Costs at most one /universe/types call per new item type and one /universe/groups call
per new group, capped per run, and only for products of manufacturing jobs.
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.esi import client as esi
from app.models import TypeInfo

log = logging.getLogger(__name__)
MAX_LOOKUPS_PER_RUN = 25


async def resolve_types(db: Session, type_ids: set[int]) -> dict[int, TypeInfo]:
    type_ids = {t for t in type_ids if t}
    known = {t.type_id: t for t in
             db.scalars(select(TypeInfo).where(TypeInfo.type_id.in_(type_ids)))}
    group_names = {t.group_id: t.group_name for t in db.scalars(
        select(TypeInfo).where(TypeInfo.group_name.is_not(None)))}
    budget = MAX_LOOKUPS_PER_RUN
    for tid in sorted(type_ids - known.keys()):
        if budget <= 0:
            break  # the rest resolve on the next run
        budget -= 1
        try:
            info = await esi.get(f"/universe/types/{tid}")
        except esi.EsiError as e:
            log.info("Type %s lookup failed: %s", tid, e)
            continue
        gid = info.get("group_id")
        gname = group_names.get(gid)
        category = None
        if gid and gname is None and budget > 0:
            budget -= 1
            try:
                g = await esi.get(f"/universe/groups/{gid}")
                gname, category = g.get("name"), g.get("category_id")
                group_names[gid] = gname
            except esi.EsiError as e:
                log.info("Group %s lookup failed: %s", gid, e)
        row = TypeInfo(type_id=tid, name=info.get("name"), group_id=gid, group_name=gname,
                       category_id=category, resolved_at=utcnow())
        db.merge(row)
        known[tid] = row
    db.flush()
    return known
