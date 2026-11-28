"""Jump freighter planning.

* Star map: every k-space system's position and security from ESI, fetched once.
* Ship numbers: base jump range, isotopes per light year and cargo from the ship's game
  data (ESI dogma), then the pilot's skills and fitted modules. Bonus sizes are settings
  with sensible defaults; any final number can be overridden with what the in-game
  fitting window shows.
* Routes: fewest jumps first, then fewest light years (= least fuel). Jumps can't end in
  high-sec, Pochven or wormholes; a high-sec destination lands in the nearest cyno-able
  system and finishes by gates.
"""
import asyncio
import heapq
import json
import logging
import math
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope, utcnow
from app.esi import client as esi
from app.models import (
    Asset, Character, CharacterSkill, Contract, CynoPoint, EntityName, JumpPilot, Location,
    MapSystem, Setting, TypeInfo,
)

log = logging.getLogger(__name__)

LY_M = 9_460_730_472_580_800          # metres in a light year
POCHVEN = 10000070
HIGHSEC = 0.45                        # in-game security rounds to 0.5 from here
JF_GROUP = "Jump Freighter"
RACE = {"Nomad": "Minmatar", "Anshar": "Gallente", "Rhea": "Caldari", "Ark": "Amarr"}
ATTR_FUEL_TYPE, ATTR_RANGE, ATTR_FUEL = 866, 867, 868
ISOTOPES = {17889: "Hydrogen Isotopes", 17887: "Oxygen Isotopes", 16274: "Helium Isotopes",
            17888: "Nitrogen Isotopes"}

# Bonus sizes (fraction per level / per module). Defaults; editable on the Pilots tab.
BONUS_DEFAULTS = {
    "range_per_jdc": 0.20,       # Jump Drive Calibration: +20% range per level
    "fuel_per_jfc": 0.10,        # Jump Fuel Conservation: -10% fuel per level
    "fuel_per_jf": 0.10,         # Jump Freighters: -10% fuel per level
    "cargo_per_racial": 0.05,    # <Race> Freighter: +5% cargo per level
    "economizer_fuel": 0.10,     # each Jump Drive Economizer: -X% fuel
    "expander_cargo": 0.275,     # each Expanded Cargohold: +X% cargo
}
BONUS_LABELS = {
    "range_per_jdc": "Jump Drive Calibration: range per level",
    "fuel_per_jfc": "Jump Fuel Conservation: fuel saved per level",
    "fuel_per_jf": "Jump Freighters: fuel saved per level",
    "cargo_per_racial": "Racial Freighter skill: cargo per level",
    "economizer_fuel": "Jump Drive Economizer: fuel saved each",
    "expander_cargo": "Expanded Cargohold: cargo added each",
}


# ------------------------------------------------------------------ settings helpers

def _get_json(db: Session, key: str, default):
    row = db.get(Setting, key)
    try:
        return json.loads(row.value) if row and row.value else default
    except ValueError:
        return default


def _set_json(db: Session, key: str, value) -> None:
    row = db.get(Setting, key) or Setting(key=key, value="")
    row.value = json.dumps(value)
    db.add(row)


def bonuses(db: Session) -> dict[str, float]:
    return {**BONUS_DEFAULTS, **_get_json(db, "jump:bonuses", {})}


def save_bonuses(db: Session, values: dict[str, float]) -> None:
    _set_json(db, "jump:bonuses", {k: v for k, v in values.items() if k in BONUS_DEFAULTS})


# ------------------------------------------------------------------ star map import

def map_status(db: Session) -> dict:
    st = _get_json(db, "jump:map_status", {})
    st["systems"] = db.query(MapSystem).count()
    return st


async def import_map(concurrency: int = 12) -> int:
    """Fetch every k-space system (position, security, region) once. ~6,500 public ESI
    calls the first time; a re-run only fetches what's missing. Returns systems added."""
    with session_scope() as db:
        _set_json(db, "jump:map_status", {"state": "running", "started": utcnow().isoformat()})
    try:
        ids = [i for i in await esi.get("/universe/systems") if i < 31_000_000]  # no w-space
        with session_scope() as db:
            have = set(db.scalars(select(MapSystem.system_id)))
        todo = [i for i in ids if i not in have]
        sem = asyncio.Semaphore(concurrency)

        async def one(path):
            async with sem:
                try:
                    return await esi.get(path)
                except esi.EsiError as e:
                    log.info("Map lookup %s failed: %s", path, e)
                    return None

        systems = []
        for start in range(0, len(todo), 500):
            batch = await asyncio.gather(*(one(f"/universe/systems/{i}") for i in todo[start:start + 500]))
            systems += [s for s in batch if s]
            with session_scope() as db:
                _set_json(db, "jump:map_status", {"state": "running", "done": len(have) + len(systems),
                                                  "total": len(ids)})
        consts = {s["constellation_id"] for s in systems}
        cinfo = dict(zip(consts, await asyncio.gather(*(one(f"/universe/constellations/{c}") for c in consts))))
        regions = {c["region_id"] for c in cinfo.values() if c}
        rinfo = dict(zip(regions, await asyncio.gather(*(one(f"/universe/regions/{r}") for r in regions))))
        with session_scope() as db:
            for s in systems:
                c = cinfo.get(s["constellation_id"]) or {}
                r = rinfo.get(c.get("region_id")) or {}
                p = s.get("position") or {}
                db.merge(MapSystem(system_id=s["system_id"], name=s["name"],
                                   constellation_id=s["constellation_id"],
                                   region_id=c.get("region_id"), region_name=r.get("name"),
                                   security=s.get("security_status", 0.0),
                                   x=p.get("x", 0.0), y=p.get("y", 0.0), z=p.get("z", 0.0)))
            _set_json(db, "jump:map_status", {"state": "done", "finished": utcnow().isoformat(),
                                              "total": len(ids)})
        _GRAPHS.clear()
        return len(systems)
    except Exception as e:  # noqa: BLE001 — show on the page, try again later
        log.exception("Star map import failed")
        with session_scope() as db:
            _set_json(db, "jump:map_status", {"state": "failed", "error": str(e)[:200]})
        return 0


# ------------------------------------------------------------------ ships and pilots

async def ship_base(db: Session, type_id: int) -> dict | None:
    """Base jump range (LY), isotopes per LY, isotope type and cargo for a ship type,
    from ESI's game data. Cached forever (static)."""
    key = f"jump:ship:{type_id}"
    cached = _get_json(db, key, None)
    if cached:
        return cached
    try:
        info = await esi.get(f"/universe/types/{type_id}")
    except esi.EsiError as e:
        log.info("Ship %s lookup failed: %s", type_id, e)
        return None
    attrs = {a["attribute_id"]: a["value"] for a in info.get("dogma_attributes", [])}
    if ATTR_RANGE not in attrs or ATTR_FUEL not in attrs:
        return None
    base = {"name": info.get("name"), "range": attrs[ATTR_RANGE], "fuel": attrs[ATTR_FUEL],
            "fuel_type": int(attrs.get(ATTR_FUEL_TYPE, 0)), "cargo": info.get("capacity", 0)}
    _set_json(db, key, base)
    db.commit()
    return base


@dataclass
class Profile:
    character_id: int
    name: str
    ship_type_id: int | None = None
    ship_name: str | None = None
    base: dict | None = None
    skills: dict[str, int] = field(default_factory=dict)
    economizers: int = 0
    expanders: int = 0
    fit_source: str = "none"          # "assets" | "manual" | "none"
    range_ly: float = 0.0
    fuel_per_ly: float = 0.0
    cargo_m3: float = 0.0
    fuel_type: int | None = None
    max_collateral: float | None = None
    overridden: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)

    @property
    def fuel_name(self) -> str:
        return ISOTOPES.get(self.fuel_type or 0, "isotopes")

    @property
    def ready(self) -> bool:
        return self.range_ly > 0 and self.fuel_per_ly > 0


def _skill_levels(db: Session, character_id: int) -> dict[str, int]:
    rows = db.execute(select(EntityName.name, CharacterSkill.active_level).join(
        EntityName, EntityName.entity_id == CharacterSkill.skill_id).where(
        CharacterSkill.character_id == character_id)).all()
    return {n: lvl for n, lvl in rows}


def detect_ship(db: Session, character_id: int) -> tuple[int | None, int, int, bool]:
    """(ship type, economizers, expanders, found) from the character's assets: the first
    jump freighter it owns, and the modules fitted in its low slots."""
    jf_types = {t.type_id for t in db.scalars(select(TypeInfo).where(TypeInfo.group_name == JF_GROUP))}
    jf_types |= {e.entity_id for e in db.scalars(select(EntityName).where(
        EntityName.name.in_(list(RACE))))}
    ship = db.scalar(select(Asset).where(Asset.owner_type == "character",
                                         Asset.owner_id == character_id,
                                         Asset.type_id.in_(jf_types)).limit(1))
    if ship is None:
        return None, 0, 0, False
    fitted = db.execute(select(EntityName.name).join(
        Asset, Asset.type_id == EntityName.entity_id).where(
        Asset.location_id == ship.item_id, Asset.location_flag.like("LoSlot%"))).scalars().all()
    econ = sum(1 for n in fitted if "Jump Drive Economizer" in n)
    exp = sum(1 for n in fitted if "Expanded Cargohold" in n)
    return ship.type_id, econ, exp, True


def profile(db: Session, pilot: JumpPilot, char: Character, bonus: dict[str, float]) -> Profile:
    p = Profile(char.character_id, char.name, max_collateral=pilot.max_collateral)
    found_type, econ, exp, found = detect_ship(db, char.character_id)
    p.ship_type_id = pilot.ship_type_id or found_type
    if p.ship_type_id:
        p.base = _get_json(db, f"jump:ship:{p.ship_type_id}", None)
        p.ship_name = (p.base or {}).get("name") or db.scalar(select(EntityName.name).where(
            EntityName.entity_id == p.ship_type_id))
    if pilot.economizers is not None or pilot.expanders is not None:
        p.economizers, p.expanders, p.fit_source = pilot.economizers or 0, pilot.expanders or 0, "manual"
    elif found:
        p.economizers, p.expanders, p.fit_source = econ, exp, "assets"
    levels = _skill_levels(db, char.character_id)
    race = RACE.get((p.ship_name or "").split()[0], "")
    p.skills = {"Jump Drive Calibration": levels.get("Jump Drive Calibration", 0),
                "Jump Fuel Conservation": levels.get("Jump Fuel Conservation", 0),
                "Jump Freighters": levels.get("Jump Freighters", 0)}
    if race:
        p.skills[f"{race} Freighter"] = levels.get(f"{race} Freighter", 0)
    if p.base:
        b = p.base
        p.fuel_type = b.get("fuel_type")
        p.range_ly = b["range"] * (1 + bonus["range_per_jdc"] * p.skills["Jump Drive Calibration"])
        p.fuel_per_ly = (b["fuel"] * (1 - bonus["fuel_per_jfc"] * p.skills["Jump Fuel Conservation"])
                         * (1 - bonus["fuel_per_jf"] * p.skills["Jump Freighters"])
                         * (1 - bonus["economizer_fuel"]) ** p.economizers)
        p.cargo_m3 = (b["cargo"] * (1 + bonus["cargo_per_racial"] * p.skills.get(f"{race} Freighter", 0))
                      * (1 + bonus["expander_cargo"]) ** p.expanders)
    elif p.ship_type_id:
        p.problems.append("Ship data not loaded yet (press Refresh ship data).")
    else:
        p.problems.append("No jump freighter found in its assets: pick the ship below.")
    if not levels:
        p.problems.append("No skills synced (Skills switch on the Characters page).")
    for attr, ov in (("range_ly", pilot.range_override), ("fuel_per_ly", pilot.fuel_override),
                     ("cargo_m3", pilot.cargo_override)):
        if ov:
            setattr(p, attr, ov)
            p.overridden.add(attr)
    return p


def profiles(db: Session, enabled_only: bool = False) -> list[Profile]:
    bonus = bonuses(db)
    out = []
    for pilot in db.scalars(select(JumpPilot)):
        char = db.get(Character, pilot.character_id)
        if char is None or (enabled_only and not pilot.enabled):
            continue
        out.append(profile(db, pilot, char, bonus))
    return sorted(out, key=lambda p: p.name.lower())


# ------------------------------------------------------------------ routing

def can_cyno(s: MapSystem) -> bool:
    """A jump can end here: low/null-sec k-space, not Pochven."""
    return s.security < HIGHSEC and s.region_id != POCHVEN and s.system_id < 31_000_000


def ly(a: MapSystem, b: MapSystem) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z)) / LY_M


class Graph:
    """Systems a jump can end in, bucketed by position so neighbours within range are
    found without comparing every pair."""

    def __init__(self, systems: list[MapSystem], cell_ly: float):
        self.cell = cell_ly * LY_M
        self.sys = {s.system_id: s for s in systems}
        self.grid: dict[tuple, list[MapSystem]] = {}
        for s in systems:
            self.grid.setdefault(self._key(s), []).append(s)

    def _key(self, s):
        return (int(s.x // self.cell), int(s.y // self.cell), int(s.z // self.cell))

    def near(self, s: MapSystem, range_ly: float):
        kx, ky, kz = self._key(s)
        r = math.ceil(range_ly * LY_M / self.cell)
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for dz in range(-r, r + 1):
                    for t in self.grid.get((kx + dx, ky + dy, kz + dz), ()):
                        if t.system_id != s.system_id:
                            d = ly(s, t)
                            if d <= range_ly:
                                yield t, d


_GRAPHS: dict[tuple, Graph] = {}


def graph(db: Session, only_cynos: bool) -> Graph:
    cyno_ids = frozenset(db.scalars(select(CynoPoint.system_id))) if only_cynos else frozenset()
    key = (only_cynos, cyno_ids)
    if key not in _GRAPHS:
        systems = [s for s in db.scalars(select(MapSystem)) if can_cyno(s)
                   and (not only_cynos or s.system_id in cyno_ids)]
        _GRAPHS.clear()
        _GRAPHS[key] = Graph(systems, cell_ly=5.0)
    return _GRAPHS[key]


@dataclass
class Leg:
    frm: MapSystem
    to: MapSystem
    ly: float
    fuel: int


@dataclass
class Route:
    origin: MapSystem
    dest: MapSystem
    legs: list[Leg] = field(default_factory=list)
    landing: MapSystem | None = None      # when the destination is high-sec
    gate_jumps: int | None = None          # gates from landing to destination (None: not looked up)
    error: str | None = None

    @property
    def jumps(self) -> int:
        return len(self.legs)

    @property
    def total_ly(self) -> float:
        return sum(leg.ly for leg in self.legs)

    @property
    def fuel(self) -> int:
        return sum(leg.fuel for leg in self.legs)


def fuel_for(distance_ly: float, fuel_per_ly: float) -> int:
    return math.ceil(distance_ly * fuel_per_ly)


def shortest(g: Graph, origin: MapSystem, targets: set[int], range_ly: float) -> list[tuple[MapSystem, float]] | None:
    """Fewest jumps, then fewest LY, from origin to any target. Returns [(system, ly)]."""
    best: dict[int, tuple[int, float]] = {origin.system_id: (0, 0.0)}
    prev: dict[int, tuple[int, float]] = {}
    nodes = {origin.system_id: origin}
    heap = [(0, 0.0, origin.system_id)]
    while heap:
        jumps, dist, sid = heapq.heappop(heap)
        if best.get(sid, (1e9, 1e18)) < (jumps, dist):
            continue
        if sid in targets and sid != origin.system_id:
            path, cur = [], sid
            while cur != origin.system_id:
                p, d = prev[cur]
                path.append((nodes[cur], d))
                cur = p
            return list(reversed(path))
        for t, d in g.near(nodes[sid], range_ly):
            cand = (jumps + 1, dist + d)
            if cand < best.get(t.system_id, (1e9, 1e18)):
                best[t.system_id] = cand
                prev[t.system_id] = (sid, d)
                nodes[t.system_id] = t
                heapq.heappush(heap, (cand[0], cand[1], t.system_id))
    return None


def plan_route(db: Session, origin_id: int, dest_id: int, p: Profile,
               only_cynos: bool = False, gates: dict[int, int] | None = None) -> Route:
    """gates: for a high-sec destination, gate jumps from candidate landing systems
    (filled by the caller from ESI /route); without it the nearest by LY is used."""
    o, d = db.get(MapSystem, origin_id), db.get(MapSystem, dest_id)
    if o is None or d is None:
        return Route(o, d, error="Unknown system (is the star map downloaded?)")
    r = Route(o, d)
    if not p.ready:
        r.error = f"{p.name}'s jump range and fuel aren't known yet."
        return r
    g = graph(db, only_cynos)
    if origin_id == dest_id:
        return r
    if can_cyno(d) and (not only_cynos or dest_id in g.sys):
        targets = {dest_id}
    else:
        cands = landing_candidates(db, d, g)
        if not cands:
            r.error = "No cyno-able system near the destination."
            return r
        if gates:
            cands = sorted(cands, key=lambda s: (gates.get(s.system_id, 99), ly(s, d)))
        r.landing = cands[0]
        r.gate_jumps = gates.get(cands[0].system_id) if gates else None
        targets = {cands[0].system_id}
        if cands[0].system_id == origin_id:
            return r
    path = shortest(g, o, targets, p.range_ly)
    if path is None:
        r.error = (f"No route within {p.range_ly:.2f} LY per jump"
                   + (" using only your cyno points." if only_cynos else "."))
        return r
    frm = o
    for s, dist in path:
        r.legs.append(Leg(frm, s, dist, fuel_for(dist, p.fuel_per_ly)))
        frm = s
    return r


def landing_candidates(db: Session, dest: MapSystem, g: Graph, limit: int = 6) -> list[MapSystem]:
    """Cyno-able systems closest (by LY) to a high-sec destination."""
    near = sorted(((ly(s, dest), s) for s in g.sys.values()), key=lambda x: x[0])
    return [s for _d, s in near[:limit]]


async def gate_counts(origins: list[int], dest: int) -> dict[int, int]:
    """Gate jumps from each candidate to the destination (ESI /route, safest path)."""
    async def one(o):
        try:
            return o, len(await esi.get(f"/route/{o}/{dest}", params={"flag": "secure"})) - 1
        except esi.EsiError:
            return o, 99
    return dict(await asyncio.gather(*(one(o) for o in origins)))


async def isotope_prices(db: Session) -> dict[int, float]:
    """Average market price per isotope type (ESI /markets/prices, refreshed daily)."""
    cached = _get_json(db, "jump:isotope_prices", {})
    if cached.get("at") and (utcnow().timestamp() - cached["at"]) < 86400:
        return {int(k): v for k, v in cached["prices"].items()}
    try:
        rows = await esi.get("/markets/prices")
    except esi.EsiError:
        return {int(k): v for k, v in cached.get("prices", {}).items()}
    prices = {r["type_id"]: r.get("average_price") or r.get("adjusted_price") or 0
              for r in rows if r["type_id"] in ISOTOPES}
    _set_json(db, "jump:isotope_prices", {"at": utcnow().timestamp(), "prices": prices})
    db.commit()
    return prices


def find_system(db: Session, text: str) -> MapSystem | None:
    text = (text or "").strip()
    if not text:
        return None
    return db.scalar(select(MapSystem).where(MapSystem.name.ilike(text))) or db.scalar(
        select(MapSystem).where(MapSystem.name.ilike(f"{text}%")).order_by(MapSystem.name).limit(1))


# ------------------------------------------------------------------ courier lanes

@dataclass
class Trip:
    contracts: list[Contract]
    volume: float
    reward: float
    collateral: float


@dataclass
class Lane:
    start: MapSystem
    end: MapSystem
    contracts: list[Contract]
    route: Route | None = None
    trips: list[Trip] = field(default_factory=list)
    unfit: list[Contract] = field(default_factory=list)   # too big / too much collateral

    @property
    def reward(self) -> float:
        return sum(c.reward_isk or 0 for c in self.contracts)

    @property
    def volume(self) -> float:
        return sum(c.volume_m3 or 0 for c in self.contracts)

    @property
    def collateral(self) -> float:
        return sum(c.collateral_isk or 0 for c in self.contracts)

    @property
    def per_jump(self) -> float:
        """ISK per jump flown for the first (best) trip: what the ranking uses."""
        if not self.trips or not self.route or self.route.error:
            return 0.0
        return self.trips[0].reward / max(1, self.route.jumps)


def pack(contracts: list[Contract], cargo: float, max_collateral: float | None) -> tuple[list[Trip], list[Contract]]:
    """Fill trips best-paying-per-m3 first, within cargo and the collateral limit."""
    def fits(c):
        return (c.volume_m3 or 0) <= cargo and (not max_collateral or (c.collateral_isk or 0) <= max_collateral)
    ok = sorted((c for c in contracts if fits(c)),
                key=lambda c: -((c.reward_isk or 0) / max(1.0, c.volume_m3 or 0)))
    unfit = [c for c in contracts if not fits(c)]
    trips: list[Trip] = []
    for c in ok:
        for t in trips:
            if (t.volume + (c.volume_m3 or 0) <= cargo and
                    (not max_collateral or t.collateral + (c.collateral_isk or 0) <= max_collateral)):
                t.contracts.append(c)
                t.volume += c.volume_m3 or 0
                t.reward += c.reward_isk or 0
                t.collateral += c.collateral_isk or 0
                break
        else:
            trips.append(Trip([c], c.volume_m3 or 0, c.reward_isk or 0, c.collateral_isk or 0))
    trips.sort(key=lambda t: -t.reward)
    return trips, unfit


def lanes(db: Session, p: Profile | None, statuses=("outstanding", "in_progress"),
          only_cynos: bool = False) -> list[Lane]:
    """Open courier contracts grouped by (pickup system, drop system), each with a route
    and trips packed for the chosen pilot; best ISK per jump first."""
    contracts = db.scalars(select(Contract).where(Contract.status.in_(statuses))).all()
    locs = {loc.location_id: loc for loc in db.scalars(select(Location).where(
        Location.location_id.in_({c.start_location_id for c in contracts}
                                 | {c.end_location_id for c in contracts})))}
    groups: dict[tuple[int, int], list[Contract]] = {}
    for c in contracts:
        a, b = locs.get(c.start_location_id), locs.get(c.end_location_id)
        if a and b and a.system_id and b.system_id:
            groups.setdefault((a.system_id, b.system_id), []).append(c)
    out = []
    for (sa, sb), cs in groups.items():
        start, end = db.get(MapSystem, sa), db.get(MapSystem, sb)
        if start is None or end is None:
            continue
        lane = Lane(start, end, cs)
        if p is not None:
            lane.route = plan_route(db, sa, sb, p, only_cynos)
            lane.trips, lane.unfit = pack(cs, p.cargo_m3 or float("inf"), p.max_collateral)
        out.append(lane)
    out.sort(key=lambda ln: (-ln.per_jump, -ln.reward))
    return out
