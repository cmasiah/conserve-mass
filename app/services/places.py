"""Which structure is a blueprint in?

ESI's blueprint lists give a `location_id` that is often NOT the structure:
  * corp hangar items in a station sit in the corp *office* (an item), and
  * anything in a container sits in that container (also an item).
The only way to climb from there to the station/structure is the owner's asset list.
So, per owner, we keep the few asset "places" that hold things (offices, containers,
ships) in asset_nodes, and fetch assets only when a blueprint is somewhere we can't place
yet (or the cached places are a day old). Station and structure names then come from the
normal location resolver.
"""
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.esi import client as esi
from app.esi import resolver
from app.models import AssetNode, Location

log = logging.getLogger(__name__)

OFFICE_TYPE_ID = 27            # "Office" folder that holds a corp's station hangars
NODES_MAX_AGE = timedelta(hours=24)
MAX_DEPTH = 6


def is_station(i: int) -> bool:
    return 60_000_000 <= i < 64_000_000


def is_system(i: int) -> bool:
    return 30_000_000 <= i < 33_000_000


def walk(location_id: int, nodes: dict[int, AssetNode]) -> tuple[int, list[AssetNode]]:
    """Climb from a blueprint's location to its station/structure.
    Returns (site id, the asset places passed on the way, innermost first)."""
    chain: list[AssetNode] = []
    cur = location_id
    for _ in range(MAX_DEPTH):
        node = nodes.get(cur)
        if node is None or is_system(node.parent_id):
            return cur, chain   # a station/structure (or something we own floating in space)
        chain.append(node)
        cur = node.parent_id
    return cur, chain


def _places_from_assets(assets: list[dict], wanted: set[int]) -> dict[int, dict]:
    """Assets that hold other assets (offices, containers, ships), plus the ones the
    blueprints point at (ESI's asset and blueprint lists can disagree for a moment)."""
    by_id = {a["item_id"]: a for a in assets}
    holders = {a["location_id"] for a in assets} | wanted
    return {i: by_id[i] for i in holders if i in by_id}


@dataclass
class LocateResult:
    fetched_assets: bool = False
    sites: set[int] = field(default_factory=set)
    unknown: set[int] = field(default_factory=set)


async def locate(db: Session, owner_type: str, owner_id: int, blueprints: list[dict], *,
                 fetch_assets: Callable[[], Awaitable[esi.EsiResult | None]] | None,
                 post_names: Callable[[list[int]], Awaitable[list[dict]]] | None,
                 token: str | None, budget: resolver.LookupBudget) -> LocateResult:
    out = LocateResult()
    loc_ids = {b["location_id"] for b in blueprints if b.get("location_id")}
    if not loc_ids:
        return out
    nodes = {n.item_id: n for n in db.scalars(select(AssetNode).where(
        AssetNode.owner_type == owner_type, AssetNode.owner_id == owner_id))}
    known_sites = set(db.scalars(select(Location.location_id).where(
        Location.location_id.in_(loc_ids))))
    # A blueprint in a personal hangar ("Hangar") sits directly in the station/structure.
    direct = {b["location_id"] for b in blueprints if b.get("location_flag") == "Hangar"}
    unplaced = {i for i in loc_ids if i not in nodes and i not in known_sites
                and i not in direct and not is_station(i)}
    stale = bool(nodes) and min(n.updated_at for n in nodes.values()) < utcnow() - NODES_MAX_AGE

    if (unplaced or stale) and fetch_assets is not None:
        res = await fetch_assets()
        if res is not None and not res.not_modified:
            places = _places_from_assets(res.data, loc_ids)
            db.execute(delete(AssetNode).where(AssetNode.owner_type == owner_type,
                                               AssetNode.owner_id == owner_id))
            now = utcnow()
            nodes = {}
            for i, a in places.items():
                nodes[i] = AssetNode(item_id=i, owner_type=owner_type, owner_id=owner_id,
                                     parent_id=a["location_id"],
                                     parent_flag=a.get("location_flag", ""),
                                     type_id=a["type_id"], updated_at=now)
                db.merge(nodes[i])
            out.fetched_assets = True
            # With a fresh asset list, anything not inside an asset IS a station/structure.
            unplaced = {i for i in unplaced if i not in nodes}
            for i in unplaced:
                site, _ = walk(i, nodes)
                if db.get(Location, site) is None:
                    db.add(Location(location_id=site, kind="unknown"))
                known_sites.add(site)
            unplaced = set()
            db.commit()

    # Container names, only for places that actually hold blueprints.
    in_chains = {n.item_id for i in loc_ids for n in walk(i, nodes)[1]}
    to_name = [i for i in in_chains if nodes[i].type_id != OFFICE_TYPE_ID and not nodes[i].name]
    if to_name and post_names is not None and out.fetched_assets:
        for start in range(0, len(to_name), 1000):
            rows, _bad = await resolver.isolate_failures(to_name[start:start + 1000], post_names)
            for r in rows:
                if r.get("item_id") in nodes and r.get("name") and r["name"] != "None":
                    nodes[r["item_id"]].name = r["name"][:128]
                    db.merge(nodes[r["item_id"]])
        db.commit()

    for i in loc_ids:
        site, chain = walk(i, nodes)
        if chain or site in known_sites or site in direct or is_station(site):
            out.sites.add(site)
        else:
            out.unknown.add(i)
    for site in out.sites:
        await resolver.resolve_location(db, site, token, budget)
    db.commit()
    return out


@dataclass
class Place:
    site: str            # station / structure name
    system: str | None
    detail: str          # division / container path, or a hint when unknown
    sort_key: str


def describe(location_id: int, flag: str, nodes: dict[int, AssetNode],
             locations: dict[int, Location], divisions: dict[str, str] | None) -> Place:
    site_id, chain = walk(location_id, nodes)
    loc = locations.get(site_id)
    bits: list[str] = []
    # Division: the CorpSAGn flag of whatever sits in the office (blueprint or container).
    inner_flags = [flag] + [n.parent_flag for n in chain]
    if divisions:
        for f in inner_flags:
            if f in divisions:
                bits.append(divisions[f])
                break
    for n in reversed(chain):
        if n.type_id != OFFICE_TYPE_ID:
            bits.append(n.name or "container")
    if loc and loc.name:
        return Place(loc.name, loc.system_name, " › ".join(bits), loc.name)
    hint = "structure name needs docking access" if (loc or chain) else \
        "location unknown: needs assets access"
    label = "Unknown structure" if (loc or chain) else "Unknown location"
    return Place(label, None, " › ".join(bits + [hint]), f"~{label}")
