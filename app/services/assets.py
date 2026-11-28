"""Asset tracking: every stack of items a linked character or corp owns, where it sits,
which places are watched for stockpiles, and material totals against min/max targets.

Sources (hourly, ESI caches assets for an hour anyway):
  * personal assets for characters with the Assets switch on;
  * corp assets for switched-on corps, read through a Director+ character with the role.
Each owner's list is replaced in one short transaction after all its pages arrive, and the
location of every stack (structure, corp division, container path) is worked out right
then, so pages and exports never call ESI.
"""
import logging
from dataclasses import dataclass, field

from collections import defaultdict

from sqlalchemy import delete, func, insert, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_scope, utcnow
from app.esi import client as esi
from app.esi import resolver
from app.models import Asset, AssetNode, AssetWatch, EntityName, Location, MaterialTarget, TypeInfo
from app.services import industry
from app.services.places import OFFICE_TYPE_ID, is_station, is_system
from app.services.type_info import resolve_types

log = logging.getLogger(__name__)

SCOPE_CHAR_ASSETS = "esi-assets.read_assets.v1"
SCOPE_CORP_ASSETS = "esi-assets.read_corporation_assets.v1"
TYPE_LOOKUPS_PER_RUN = 300     # one-time static lookups (item group/category); rest next hour
MAX_DEPTH = 8


# ------------------------------------------------------------------ placing stacks

def place_rows(owner_type: str, owner_id: int, raw: list[dict]) -> list[dict]:
    """ESI asset rows -> Asset rows with site / division / container path filled in."""
    by_id = {a["item_id"]: a for a in raw}
    now = utcnow()
    out = []
    for a in raw:
        division = a.get("location_flag", "") if a.get("location_flag", "").startswith(
            "CorpSAG") else ""
        path: list[int] = []
        loc = a["location_id"]
        for _ in range(MAX_DEPTH):
            parent = by_id.get(loc)
            if parent is None or is_system(parent["location_id"]):
                break           # loc is a station/structure (or something of ours in space)
            if parent["type_id"] != OFFICE_TYPE_ID:
                path.append(loc)  # a container or ship
            pflag = parent.get("location_flag", "")
            if not division and pflag.startswith("CorpSAG"):
                division = pflag
            loc = parent["location_id"]
        path.reverse()          # outermost first
        # ESI codes a blueprint's quantity: -1 = original, -2 = copy (each is one item).
        qty = a.get("quantity", 1)
        is_bpc = bool(a.get("is_blueprint_copy")) or qty == -2
        out.append({
            "item_id": a["item_id"], "owner_type": owner_type, "owner_id": owner_id,
            "type_id": a["type_id"], "quantity": qty if qty > 0 else 1, "is_bpc": is_bpc,
            "location_id": a["location_id"], "location_flag": a.get("location_flag", ""),
            "is_singleton": bool(a.get("is_singleton")), "site_id": loc, "division": division,
            "container_id": path[-1] if path else None,
            "container_path": ("," + ",".join(map(str, path)) + ",") if path else "",
            "updated_at": now,
        })
    return out


def _holders(raw: list[dict]) -> dict[int, dict]:
    by_id = {a["item_id"]: a for a in raw}
    return {i: by_id[i] for i in {a["location_id"] for a in raw} if i in by_id}


def store(db: Session, owner_type: str, owner_id: int, raw: list[dict]) -> int:
    """Replace one owner's assets (and its asset places, keeping known container names)."""
    rows = place_rows(owner_type, owner_id, raw)
    names = {n.item_id: n.name for n in db.scalars(select(AssetNode).where(
        AssetNode.owner_type == owner_type, AssetNode.owner_id == owner_id)) if n.name}
    db.execute(delete(Asset).where(Asset.owner_type == owner_type, Asset.owner_id == owner_id))
    db.execute(delete(AssetNode).where(AssetNode.owner_type == owner_type,
                                       AssetNode.owner_id == owner_id))
    if rows:
        # An item moved or traded between owners can briefly sit in both owners' lists
        # (the other one hasn't re-synced yet); the list being stored now wins.
        ids = [r["item_id"] for r in rows]
        for i in range(0, len(ids), 900):
            db.execute(delete(Asset).where(Asset.item_id.in_(ids[i:i + 900])))
        db.execute(insert(Asset), rows)
    now = utcnow()
    nodes = [{"item_id": i, "owner_type": owner_type, "owner_id": owner_id,
              "parent_id": a["location_id"], "parent_flag": a.get("location_flag", ""),
              "type_id": a["type_id"], "name": names.get(i), "updated_at": now}
             for i, a in _holders(raw).items()]
    if nodes:
        ids = [n["item_id"] for n in nodes]
        for i in range(0, len(ids), 900):
            db.execute(delete(AssetNode).where(AssetNode.item_id.in_(ids[i:i + 900])))
        db.execute(insert(AssetNode), nodes)
    return len(rows)


async def _name_containers(db: Session, owner_type: str, owner_id: int, post) -> None:
    """In-game names for containers and ships that hold things (one call per 1000)."""
    todo = [n.item_id for n in db.scalars(select(AssetNode).where(
        AssetNode.owner_type == owner_type, AssetNode.owner_id == owner_id,
        AssetNode.name.is_(None), AssetNode.type_id != OFFICE_TYPE_ID))]
    for start in range(0, len(todo), 1000):
        rows, _bad = await resolver.isolate_failures(todo[start:start + 1000], post)
        for r in rows:
            if r.get("name") and r["name"] != "None":
                node = db.get(AssetNode, r.get("item_id"))
                if node is not None:
                    node.name = r["name"][:128]
    db.commit()


def _char_names(db, ids: list[int]) -> list[str]:
    from app.models import Character
    return [(c.name if (c := db.get(Character, i)) else str(i)) for i in ids]


# ------------------------------------------------------------------ sync

async def sync_assets() -> None:
    with session_scope() as db:
        sites: set[int] = set()
        structure_token: str | None = None
        for tok in industry._personal_tokens(db, SCOPE_CHAR_ASSETS, "assets"):
            cid = tok.character_id
            try:
                token = await esi.access_token(db, cid)
                res = await esi.get_cached_paged(db, f"assetsync:char:{cid}",
                                                 f"/characters/{cid}/assets", token=token)
            except (esi.EsiError, esi.NoValidToken) as e:
                log.warning("Asset sync failed for %s: %s", tok.character.name, e)
                db.commit()
                continue
            if not res.not_modified:
                n = store(db, "character", cid, res.data)
                log.info("Assets for %s: %d stacks", tok.character.name, n)
            db.commit()   # this owner's assets + ETag together; lock released

            async def post(ids, cid=cid, token=token):
                return await esi.post(f"/characters/{cid}/assets/names", ids, token=token)
            await _name_containers(db, "character", cid, post)
            if industry.SCOPE_STRUCTURES in tok.scope_set:
                structure_token = structure_token or token

        for corp_id in industry._tracked_corp_ids(db):
            try:
                res = await industry._corp_get(db, corp_id, SCOPE_CORP_ASSETS,
                                               f"assetsync:corp:{corp_id}",
                                               f"/corporations/{corp_id}/assets")
            except esi.EsiError as e:
                log.warning("Corp asset sync failed for %s: %s", corp_id, e)
                db.commit()
                continue
            if res is None:
                continue  # no Director+ character with the Director role
            if not res.not_modified:
                n = store(db, "corporation", corp_id, res.data)
                log.info("Corp assets for %s: %d stacks", corp_id, n)
            db.commit()

            async def cpost(ids, corp_id=corp_id):
                token = await industry._corp_token(db, corp_id, SCOPE_CORP_ASSETS)
                if not token:
                    return []
                return await esi.post(f"/corporations/{corp_id}/assets/names", ids, token=token)
            await _name_containers(db, "corporation", corp_id, cpost)
            structure_token = structure_token or await industry._corp_token(
                db, corp_id, industry.SCOPE_STRUCTURES)

        # Names: item types (batched), stations/structures (capped per run).
        type_ids = set(db.scalars(select(Asset.type_id).distinct()))
        await resolver.resolve_names(db, type_ids)
        sites = set(db.scalars(select(Asset.site_id).distinct()))
        budget = resolver.LookupBudget(get_settings().max_location_lookups_per_sync)
        owners: dict[int, list[int]] = {}
        for site, oid in db.execute(select(Asset.site_id, Asset.owner_id)
                                    .where(Asset.owner_type == "character").distinct()):
            owners.setdefault(site, []).append(oid)
        for site in sorted(sites, key=lambda s: not is_station(s)):
            if not is_system(site):
                # The items' owner first: they're the one most likely to have access.
                who = owners.get(site, [])
                await resolver.resolve_location(
                    db, site, None if who else structure_token, budget, prefer=who,
                    source="assets of " + (", ".join(_char_names(db, who)) if who else "a corp"))
        # Item groups (for the Materials page): watched items first, then everything else.
        watched = set(db.scalars(select(Asset.type_id).where(watch_filter(db)).distinct()))
        await resolve_types(db, watched, budget=TYPE_LOOKUPS_PER_RUN)
        await resolve_types(db, type_ids, budget=TYPE_LOOKUPS_PER_RUN // 3)
        db.commit()


# ------------------------------------------------------------------ watches

def watch_filter(db: Session):
    """SQL condition matching assets inside any watched place (false if none)."""
    conds = []
    watches = db.scalars(select(AssetWatch)).all()
    systems = [w.target_id for w in watches if w.kind == "system"]
    if systems:
        site_ids = select(Location.location_id).where(Location.system_id.in_(systems))
        conds.append(Asset.site_id.in_(site_ids))
        conds.append(Asset.site_id.in_(systems))
    for w in watches:
        if w.kind == "site":
            conds.append(Asset.site_id == w.target_id)
        elif w.kind == "division":
            conds.append((Asset.site_id == w.target_id) & (Asset.owner_type == "corporation")
                         & (Asset.owner_id == w.owner_id) & (Asset.division == w.division))
        elif w.kind == "personal":
            conds.append((Asset.site_id == w.target_id) & (Asset.owner_type == "character")
                         & (Asset.owner_id == w.owner_id))
        elif w.kind == "container":
            conds.append(Asset.container_path.like(f"%,{w.target_id},%"))
    return or_(*conds) if conds else Asset.item_id.is_(None)


def toggle_watch(db: Session, kind: str, target_id: int, owner_id: int | None = None,
                 division: str = "", label: str = "") -> bool:
    """Watch or unwatch a place. Returns True if it's now watched."""
    q = select(AssetWatch).where(AssetWatch.kind == kind, AssetWatch.target_id == target_id,
                                 AssetWatch.division == (division or ""))
    if owner_id is not None:
        q = q.where(AssetWatch.owner_id == owner_id)
    existing = db.scalar(q)
    if existing:
        db.delete(existing)
        return False
    db.add(AssetWatch(kind=kind, target_id=target_id, owner_id=owner_id,
                      division=division or "", label=label[:200]))
    return True


# ------------------------------------------------------------------ materials

# Materials page groups: (label, match on ESI group name, or category id)
MATERIAL_GROUPS: list[tuple[str, set[str], set[int]]] = [
    ("Minerals", {"Mineral"}, set()),
    ("Ore", set(), {25}),
    ("Ice products", {"Ice Product"}, set()),
    ("Moon materials", {"Moon Materials"}, set()),
    ("Gas", {"Harvestable Cloud", "Compressed Gas"}, set()),
    ("Reaction materials", {"Intermediate Materials", "Composite", "Hybrid Polymers",
                            "Biochemical Material", "Molecular-Forged Materials",
                            "Unrefined Mineral"}, set()),
    ("Fuel", {"Fuel Block"}, set()),
    ("Construction components", {"Construction Components", "Hybrid Tech Components",
                                 "Advanced Capital Construction Components",
                                 "Structure Components", "Subsystem Components"}, set()),
    ("Capital components", {"Capital Construction Components"}, set()),
    ("Planetary materials", set(), {42, 43}),
    ("Salvage", {"Salvaged Materials", "Ancient Salvage"}, set()),
]
OTHER_MATERIALS = "Other materials"


def material_group(ti: TypeInfo | None) -> str | None:
    """Which Materials section an item belongs to; None = not a material (ships, modules)."""
    if ti is None:
        return None
    for label, groups, cats in MATERIAL_GROUPS:
        if (ti.group_name in groups) or (ti.category_id in cats):
            return label
    if ti.category_id in (4, 17):  # Material, Commodity
        return OTHER_MATERIALS
    return None


@dataclass
class Line:
    type_id: int
    name: str
    quantity: int = 0
    stacks: int = 0
    places: dict[str, int] = field(default_factory=dict)
    group: str | None = None
    min_qty: int | None = None
    max_qty: int | None = None

    @property
    def short(self) -> int:
        return max(0, (self.min_qty or 0) - self.quantity) if self.min_qty else 0

    @property
    def excess(self) -> int:
        return max(0, self.quantity - self.max_qty) if self.max_qty is not None else 0


def type_names(db: Session, type_ids: set[int]) -> dict[int, str]:
    names = {e.entity_id: e.name for e in db.scalars(
        select(EntityName).where(EntityName.entity_id.in_(type_ids)))}
    for t in db.scalars(select(TypeInfo).where(TypeInfo.type_id.in_(type_ids))):
        if t.name and t.type_id not in names:
            names[t.type_id] = t.name
    return names


def clipboard_text(lines: list[tuple[str, int]]) -> str:
    """`Name Quantity` per line: what Janice, Ravworks and EVE's multibuy accept."""
    return "\n".join(f"{name} {qty}" for name, qty in lines if qty > 0)


def material_lines(db: Session, scope: str) -> tuple[dict[str, list[Line]], list[Line]]:
    """Totals per material section for watched places (or everything), with targets.
    Returns (sections, every line)."""
    stmt = select(Asset.type_id, func.sum(Asset.quantity), func.count()).group_by(Asset.type_id)
    if scope != "all":
        stmt = stmt.where(watch_filter(db))
    totals = {tid: (int(qty or 0), n) for tid, qty, n in db.execute(stmt)}
    targets = {t.type_id: t for t in db.scalars(select(MaterialTarget))}
    type_ids = set(totals) | set(targets)
    infos = {t.type_id: t for t in db.scalars(select(TypeInfo).where(TypeInfo.type_id.in_(type_ids)))}
    names = type_names(db, type_ids)
    sections: dict[str, list[Line]] = defaultdict(list)
    every = []
    for tid in type_ids:
        ti = infos.get(tid)
        group = material_group(ti) if ti else "Not categorized yet"
        if group is None and tid not in targets:
            continue  # ships, modules, ... aren't materials
        qty, n = totals.get(tid, (0, 0))
        t = targets.get(tid)
        ln = Line(tid, names.get(tid, f"Type {tid}"), qty, n, group=group or "Other",
                      min_qty=t.min_qty if t else None, max_qty=t.max_qty if t else None)
        sections[ln.group].append(ln)
        every.append(ln)
    order = [g for g, _gs, _cs in MATERIAL_GROUPS] + [OTHER_MATERIALS, "Other",
                                                          "Not categorized yet"]
    ordered = {g: sorted(sections[g], key=lambda x: x.name.lower()) for g in order if sections.get(g)}
    return ordered, every
