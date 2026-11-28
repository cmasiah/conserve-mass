"""Item type -> group / market group lookups via public ESI endpoints, cached permanently.

Static game data, so each type, group and market group is fetched once and never again.
Calls are capped per run; the rest resolve on later runs.
"""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.esi import client as esi
from app.models import MarketGroup, TypeInfo

log = logging.getLogger(__name__)
MAX_LOOKUPS_PER_RUN = 25
NO_MARKET_GROUP = 0


async def resolve_types(db: Session, type_ids: set[int],
                        budget: int = MAX_LOOKUPS_PER_RUN) -> dict[int, TypeInfo]:
    """Type name, group and market group. Types cached before market groups were tracked
    (market_group_id NULL) are looked up once more to fill it in."""
    type_ids = {t for t in type_ids if t}
    known = {t.type_id: t for t in
             db.scalars(select(TypeInfo).where(TypeInfo.type_id.in_(type_ids)))}
    group_names = {t.group_id: t.group_name for t in db.scalars(
        select(TypeInfo).where(TypeInfo.group_name.is_not(None)))}
    todo = sorted((type_ids - known.keys())
                  | {t for t, row in known.items() if row.market_group_id is None})
    for tid in todo:
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
        category = known[tid].category_id if tid in known else None
        if gid and gname is None and budget > 0:
            budget -= 1
            try:
                g = await esi.get(f"/universe/groups/{gid}")
                gname, category = g.get("name"), g.get("category_id")
                group_names[gid] = gname
            except esi.EsiError as e:
                log.info("Group %s lookup failed: %s", gid, e)
        row = TypeInfo(type_id=tid, name=info.get("name"), group_id=gid, group_name=gname,
                       category_id=category,
                       market_group_id=info.get("market_group_id") or NO_MARKET_GROUP,
                       resolved_at=utcnow())
        known[tid] = db.merge(row)
    db.flush()
    return known


async def resolve_market_groups(db: Session, group_ids: set[int], budget: int = 100) -> int:
    """Fetch market groups and all their parents (to the top of the tree). Returns calls."""
    have = {g.market_group_id: g for g in db.scalars(select(MarketGroup))}
    todo = [g for g in group_ids if g and g not in have]
    calls = 0
    while todo and calls < budget:
        gid = todo.pop()
        if gid in have:
            continue
        calls += 1
        try:
            info = await esi.get(f"/markets/groups/{gid}")
        except esi.EsiError as e:
            log.info("Market group %s lookup failed: %s", gid, e)
            continue
        row = MarketGroup(market_group_id=gid, name=info.get("name") or str(gid),
                          parent_id=info.get("parent_group_id"))
        have[gid] = db.merge(row)
        if row.parent_id and row.parent_id not in have:
            todo.append(row.parent_id)
    db.flush()
    return calls


def market_path(groups: dict[int, MarketGroup], gid: int | None) -> list[str]:
    """Names from the top of the market tree down to `gid`."""
    path: list[str] = []
    seen = set()
    while gid and gid in groups and gid not in seen:
        seen.add(gid)
        path.append(groups[gid].name)
        gid = groups[gid].parent_id
    return list(reversed(path))
