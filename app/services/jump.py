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

# Bonus sizes (fraction per level / per module) and module volume. Defaults; "Refresh ship
# data" reads the module numbers from the game data, and all are editable on Pilots & ships.
BONUS_DEFAULTS = {
    "range_per_jdc": 0.20,        # Jump Drive Calibration: +20% range per level
    "fuel_per_jfc": 0.10,         # Jump Fuel Conservation: -10% fuel per level
    "fuel_per_jf": 0.10,          # Jump Freighters: -10% fuel per level
    "cargo_per_racial": 0.05,     # <Race> Freighter: +5% cargo per level
    "economizer_fuel": 0.10,      # each Jump Drive Economizer: -X% fuel
    "ore_expander_cargo": 0.275,  # each ORE Expanded Cargohold: +X% cargo
    "t2_expander_cargo": 0.275,   # each Expanded Cargohold II: +X% cargo
    "economizer_volume": 10500,   # m3 an unfitted economizer takes in the hold
}
BONUS_LABELS = {
    "range_per_jdc": "Jump Drive Calibration: range per level",
    "fuel_per_jfc": "Jump Fuel Conservation: fuel saved per level",
    "fuel_per_jf": "Jump Freighters: fuel saved per level",
    "cargo_per_racial": "Racial Freighter skill: cargo per level",
    "economizer_fuel": "Jump Drive Economizer: fuel saved each",
    "ore_expander_cargo": "ORE Expanded Cargohold: cargo added each",
    "t2_expander_cargo": "Expanded Cargohold II: cargo added each",
    "economizer_volume": "Economizer carried in the hold: volume each",
}
BONUS_UNITS = {k: "%" for k in BONUS_DEFAULTS} | {"economizer_volume": "m³"}
LOW_SLOTS = 3
MODULE_NAMES = {"economizer": "Prototype Jump Drive Economizer",
                "ore": "ORE Expanded Cargohold", "t2": "Expanded Cargohold II"}


def module_kind(name: str) -> str | None:
    """Which low-slot module a fitted item is (by name)."""
    if "Jump Drive Economizer" in name:
        return "economizer"
    if "Expanded Cargohold" in name:
        return "ore" if name.startswith("ORE ") else "t2"
    return None


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
    ore: int = 0                      # ORE Expanded Cargoholds fitted
    t2: int = 0                       # Expanded Cargohold IIs fitted
    fit_source: str = "none"          # "assets" | "manual" | "none"
    hull_fuel: float = 0.0            # isotopes/LY after skills, before modules
    hull_cargo: float = 0.0           # m3 after skills, before modules
    bonus: dict = field(default_factory=dict)
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

    def fuel_with(self, economizers: int) -> float:
        """Isotopes per LY with this many economizers fitted (for a leg's own fit)."""
        if not self.hull_fuel:
            return self.fuel_per_ly
        return self.hull_fuel * (1 - self.bonus["economizer_fuel"]) ** economizers

    def cargo_with(self, ore: int, t2: int = 0) -> float:
        if not self.hull_cargo:
            return self.cargo_m3
        return (self.hull_cargo * (1 + self.bonus["ore_expander_cargo"]) ** ore
                * (1 + self.bonus["t2_expander_cargo"]) ** t2)


def _skill_levels(db: Session, character_id: int) -> dict[str, int]:
    rows = db.execute(select(EntityName.name, CharacterSkill.active_level).join(
        EntityName, EntityName.entity_id == CharacterSkill.skill_id).where(
        CharacterSkill.character_id == character_id)).all()
    return {n: lvl for n, lvl in rows}


def detect_ship(db: Session, character_id: int) -> tuple[int | None, dict[str, int], bool]:
    """(ship type, {economizer/ore/t2: count}, found) from the character's assets: the first
    jump freighter it owns, and what's fitted in its low slots."""
    jf_types = {t.type_id for t in db.scalars(select(TypeInfo).where(TypeInfo.group_name == JF_GROUP))}
    jf_types |= {e.entity_id for e in db.scalars(select(EntityName).where(
        EntityName.name.in_(list(RACE))))}
    ship = db.scalar(select(Asset).where(Asset.owner_type == "character",
                                         Asset.owner_id == character_id,
                                         Asset.type_id.in_(jf_types)).limit(1))
    fit = {"economizer": 0, "ore": 0, "t2": 0}
    if ship is None:
        return None, fit, False
    fitted = db.execute(select(EntityName.name).join(
        Asset, Asset.type_id == EntityName.entity_id).where(
        Asset.location_id == ship.item_id, Asset.location_flag.like("LoSlot%"))).scalars().all()
    for n in fitted:
        kind = module_kind(n)
        if kind:
            fit[kind] += 1
    return ship.type_id, fit, True


def profile(db: Session, pilot: JumpPilot, char: Character, bonus: dict[str, float]) -> Profile:
    p = Profile(char.character_id, char.name, max_collateral=pilot.max_collateral)
    found_type, fit, found = detect_ship(db, char.character_id)
    p.bonus = bonus
    p.ship_type_id = pilot.ship_type_id or found_type
    if p.ship_type_id:
        p.base = _get_json(db, f"jump:ship:{p.ship_type_id}", None)
        p.ship_name = (p.base or {}).get("name") or db.scalar(select(EntityName.name).where(
            EntityName.entity_id == p.ship_type_id))
    if pilot.economizers is not None or pilot.expanders is not None:
        # manual fit: economizers + ORE expanders (+ the rest as Expanded Cargohold IIs)
        p.economizers, p.ore = pilot.economizers or 0, pilot.expanders or 0
        p.t2 = max(0, LOW_SLOTS - p.economizers - p.ore) if pilot.t2_expanders is None \
            else pilot.t2_expanders
        p.fit_source = "manual"
    elif found:
        p.economizers, p.ore, p.t2, p.fit_source = fit["economizer"], fit["ore"], fit["t2"], "assets"
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
        p.hull_fuel = (b["fuel"] * (1 - bonus["fuel_per_jfc"] * p.skills["Jump Fuel Conservation"])
                       * (1 - bonus["fuel_per_jf"] * p.skills["Jump Freighters"]))
        p.hull_cargo = b["cargo"] * (1 + bonus["cargo_per_racial"] * p.skills.get(f"{race} Freighter", 0))
        p.fuel_per_ly = p.fuel_with(p.economizers)
        p.cargo_m3 = p.cargo_with(p.ore, p.t2)
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


# ------------------------------------------------------------------ module game data

async def _attr_names(db: Session, ids: set[int]) -> dict[int, str]:
    known = {int(k): v for k, v in _get_json(db, "jump:attr_names", {}).items()}
    for i in sorted(ids - known.keys()):
        try:
            known[i] = (await esi.get(f"/dogma/attributes/{i}")).get("name", "")
        except esi.EsiError:
            known[i] = ""
    _set_json(db, "jump:attr_names", known)
    return known


async def refresh_modules(db: Session) -> dict[str, str]:
    """Read the economizer's fuel saving and volume and both expanders' cargo bonus from
    the game data, and store them as the bonus values. Returns what was found."""
    found: dict[str, str] = {}
    try:
        res = await esi.post("/universe/ids", list(MODULE_NAMES.values()))
    except esi.EsiError:
        return found
    ids = {t["name"]: t["id"] for t in res.get("inventory_types", [])}
    values = bonuses(db)
    for kind, name in MODULE_NAMES.items():
        tid = ids.get(name)
        if not tid:
            continue
        try:
            info = await esi.get(f"/universe/types/{tid}")
        except esi.EsiError:
            continue
        attrs = {a["attribute_id"]: a["value"] for a in info.get("dogma_attributes", [])}
        names = await _attr_names(db, set(attrs))
        by_name = {names.get(i, ""): v for i, v in attrs.items()}
        if kind == "economizer":
            v = next((v for n, v in by_name.items() if "jumpDriveConsumption" in n or
                      "JumpDriveConsumption" in n), None)
            if v is not None and -100 < v < 0:
                values["economizer_fuel"] = -v / 100
                found["economizer_fuel"] = f"{-v:g}%"
            elif v is not None and 0 < v < 1:
                values["economizer_fuel"] = 1 - v
                found["economizer_fuel"] = f"{(1 - v) * 100:g}%"
            vol = info.get("packaged_volume") or info.get("volume")
            if vol:
                values["economizer_volume"] = vol
                found["economizer_volume"] = f"{vol:,.0f} m³"
        else:
            mult = by_name.get("cargoCapacityMultiplier")
            if mult and mult > 1:
                key = f"{kind}_expander_cargo"
                values[key] = mult - 1
                found[key] = f"+{(mult - 1) * 100:g}%"
    save_bonuses(db, values)
    _set_json(db, "jump:module_source", found)
    db.commit()
    return found


# ------------------------------------------------------------------ bulk haul ledger

import re as _re  # noqa: E402

_VOL_RE = _re.compile(r"([\d][\d,.\s]*)\s*m(?:3|³)\b", _re.I)
_NUM_RE = _re.compile(r"^\d[\d,\s]*$")


@dataclass
class Item:
    name: str
    qty: int
    unit_m3: float | None = None   # per unit; None until looked up

    @property
    def m3(self) -> float:
        return (self.unit_m3 or 0) * self.qty


def _num(text: str) -> float:
    t = text.strip().replace(" ", "").replace(" ", "")
    if "," in t and "." in t:              # 1,234.56 or 1.234,56
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", "") if _re.search(r",\d{3}$", t) else t.replace(",", ".")
    return float(t)


def parse_items(text: str) -> tuple[list[Item], list[str]]:
    """Paste from an EVE inventory window (tab separated: name, quantity, ..., volume) or
    plain lines like `Tritanium 1,000,000` / `Tritanium x1000`. Same items are summed.
    Returns (items, lines that couldn't be read)."""
    items: dict[str, Item] = {}
    bad: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        cols = [c.strip() for c in raw.split("\t")]
        name, qty, vol = None, None, None
        if len(cols) > 1:
            name = cols[0]
            for c in cols[1:]:
                m = _VOL_RE.fullmatch(c) or (_VOL_RE.search(c) if "m3" in c.lower() or "m³" in c else None)
                if m and vol is None:
                    vol = _num(m.group(1))
                elif qty is None and _NUM_RE.match(c):
                    qty = int(_num(c))
            qty = qty or 1            # assembled items show no quantity
        else:
            m = _re.match(r"^(.*?)\s+[xX×]?\s*(\d[\d,.\s]*)$", line)
            if m:
                name, qty = m.group(1).strip(), int(_num(m.group(2)))
            else:
                m = _re.match(r"^(\d[\d,.\s]*)\s*[xX×]?\s+(.+)$", line)
                if m:
                    name, qty = m.group(2).strip(), int(_num(m.group(1)))
        if not name or not qty or qty < 0:
            bad.append(raw)
            continue
        it = items.get(name.lower())
        if it is None:
            it = items[name.lower()] = Item(name, 0)
        it.qty += qty
        if vol is not None and qty:
            it.unit_m3 = vol / qty if it.unit_m3 is None else it.unit_m3
    return list(items.values()), bad


async def fill_volumes(db: Session, items: list[Item]) -> list[str]:
    """Look up the packaged volume per unit for items pasted without one (ESI, cached)."""
    cache = _get_json(db, "jump:volumes", {})
    missing = [it for it in items if it.unit_m3 is None]
    for it in missing:
        if it.name.lower() in cache:
            it.unit_m3 = cache[it.name.lower()]
    todo = [it for it in missing if it.unit_m3 is None]
    unknown: list[str] = []
    for start in range(0, len(todo), 500):
        chunk = todo[start:start + 500]
        try:
            res = await esi.post("/universe/ids", [it.name for it in chunk])
        except esi.EsiError:
            unknown += [it.name for it in chunk]
            continue
        ids = {t["name"].lower(): t["id"] for t in res.get("inventory_types", [])}
        for it in chunk:
            tid = ids.get(it.name.lower())
            if not tid:
                unknown.append(it.name)
                continue
            try:
                info = await esi.get(f"/universe/types/{tid}")
            except esi.EsiError:
                unknown.append(it.name)
                continue
            it.unit_m3 = info.get("packaged_volume") or info.get("volume") or 0
            cache[it.name.lower()] = it.unit_m3
    _set_json(db, "jump:volumes", cache)
    db.commit()
    return unknown


@dataclass
class Hauler:
    profile: Profile
    loaded_ore: int = LOW_SLOTS          # ORE expanders fitted for the loaded leg
    return_econ: int = LOW_SLOTS          # economizers fitted for the empty leg back
    capacity_override: float | None = None

    @property
    def loaded_econ(self) -> int:         # whatever low slots aren't ORE expanders
        return max(0, LOW_SLOTS - self.loaded_ore)

    @property
    def carried_econ(self) -> int:        # economizers riding in the hold, for the way back
        return max(0, self.return_econ - self.loaded_econ)

    @property
    def capacity(self) -> float:
        if self.capacity_override:
            return self.capacity_override
        cargo = self.profile.cargo_with(self.loaded_ore, 0)
        return max(0.0, cargo - self.carried_econ * self.profile.bonus.get(
            "economizer_volume", BONUS_DEFAULTS["economizer_volume"]))

    @property
    def fuel_loaded(self) -> float:
        return self.profile.fuel_with(self.loaded_econ)

    @property
    def fuel_empty(self) -> float:
        return self.profile.fuel_with(self.return_econ)


@dataclass
class Load:
    hauler: Hauler
    wave: int
    items: list[tuple[str, int, float]] = field(default_factory=list)   # name, qty, m3

    @property
    def m3(self) -> float:
        return sum(m for _n, _q, m in self.items)

    @property
    def free(self) -> float:
        return self.hauler.capacity - self.m3

    def clip(self) -> str:
        return "\n".join(f"{n} {q}" for n, q, _m in self.items)


@dataclass
class HaulPlan:
    loads: list[Load] = field(default_factory=list)
    too_big: list[Item] = field(default_factory=list)
    total_m3: float = 0.0
    routes: dict[int, Route] = field(default_factory=dict)       # per pilot
    round_trip: bool = True

    @property
    def waves(self) -> int:
        return max((ld.wave for ld in self.loads), default=0)

    def jumps_for(self, ld: Load) -> int:
        r = self.routes.get(ld.hauler.profile.character_id)
        if not r or r.error:
            return 0
        return r.jumps * (2 if self.round_trip else 1)

    def fuel_for(self, ld: Load) -> int:
        r = self.routes.get(ld.hauler.profile.character_id)
        if not r or r.error:
            return 0
        there = fuel_for(r.total_ly, ld.hauler.fuel_loaded)
        back = fuel_for(r.total_ly, ld.hauler.fuel_empty) if self.round_trip else 0
        return there + back

    @property
    def total_jumps(self) -> int:
        return sum(self.jumps_for(ld) for ld in self.loads)

    def fuel_by_type(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for ld in self.loads:
            t = ld.hauler.profile.fuel_type or 0
            out[t] = out.get(t, 0) + self.fuel_for(ld)
        return out


def _last_wave(haulers: list[Hauler], remaining: float, plan: HaulPlan) -> list[Hauler]:
    """For the final, partial wave: the fewest JFs that can carry what's left, and of
    those the one(s) burning the least fuel."""
    from itertools import combinations
    best = None
    for n in range(1, len(haulers) + 1):
        for combo in combinations(haulers, n):
            if sum(h.capacity for h in combo) + 1e-6 < remaining:
                continue
            fuel = 0
            for h in combo:
                r = plan.routes.get(h.profile.character_id)
                if r and not r.error:
                    fuel += fuel_for(r.total_ly, h.fuel_loaded) + (
                        fuel_for(r.total_ly, h.fuel_empty) if plan.round_trip else 0)
            key = (fuel, -sum(h.capacity for h in combo))
            if best is None or key < best[0]:
                best = (key, list(combo))
        if best:
            return sorted(best[1], key=lambda h: -h.capacity)
    return sorted(haulers, key=lambda h: -h.capacity)


SCRAP_M3 = 1000     # don't split a stack into a top-off smaller than this


def plan_haul(items: list[Item], haulers: list[Hauler],
              routes: dict[int, Route] | None = None, round_trip: bool = True) -> HaulPlan:
    """Fewest loaded trips for the whole job: every JF hauls in parallel waves; the last
    wave uses the fewest (then cheapest) JFs. Stacks stay whole where they fit and are
    split only when needed."""
    plan = HaulPlan(routes=routes or {}, round_trip=round_trip)
    haulers = [h for h in haulers if h.capacity > 0]
    if not haulers:
        return plan
    biggest = max(h.capacity for h in haulers)
    goods = []
    for it in items:
        if (it.unit_m3 or 0) > biggest:
            plan.too_big.append(it)
        elif it.qty > 0:
            goods.append(Item(it.name, it.qty, it.unit_m3 or 0))
    plan.total_m3 = sum(it.m3 for it in goods)
    remaining = plan.total_m3
    loads: list[Load] = []
    wave = 0
    fleet = sum(h.capacity for h in haulers)
    while remaining > 1e-6:
        wave += 1
        crew = haulers if remaining > fleet else _last_wave(haulers, remaining, plan)
        for h in sorted(crew, key=lambda h: -h.capacity):
            loads.append(Load(h, wave))
        remaining -= sum(h.capacity for h in crew)
    # Bulky-per-unit items first (kept whole where they fit), fine-grained stacks like
    # minerals last as filler: those split cleanly into whatever space is left.
    goods.sort(key=lambda it: (-(it.unit_m3 or 0), -it.m3))
    for it in goods:
        qty = it.qty
        whole = next((ld for ld in loads if ld.free + 1e-6 >= it.unit_m3 * qty), None)
        if whole:
            whole.items.append((it.name, qty, it.unit_m3 * qty))
            continue
        # Top off part-filled holds first, but never leave a scrap (a few units
        # topping off an almost-full hold) unless it finishes the stack.
        for ld in sorted(loads, key=lambda ld: ld.free):
            if qty <= 0:
                break
            can = int((ld.free + 1e-6) // it.unit_m3) if it.unit_m3 else qty
            take = min(qty, can)
            if take <= 0 or (take < qty and take * it.unit_m3 < SCRAP_M3):
                continue
            ld.items.append((it.name, take, it.unit_m3 * take))
            qty -= take
        while qty > 0:  # what didn't fit: more loads on the roomiest JF
            h = max(haulers, key=lambda h: h.capacity)
            wave += 1
            extra = Load(h, wave)
            take = min(qty, int(h.capacity // it.unit_m3) if it.unit_m3 else qty) or qty
            extra.items.append((it.name, take, it.unit_m3 * take))
            loads.append(extra)
            qty -= take
    plan.loads = [ld for ld in loads if ld.items]
    return plan


# ------------------------------------------------------------------ system search

def _squash(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def search_systems(db: Session, q: str, limit: int = 12) -> list[dict]:
    """Fuzzy: exact > starts with > contains > letters in order (ignores dashes/case).
    'ac2e' finds AC2E-3; 'jta' finds Jita."""
    q = _squash(q)
    if not q:
        return []
    scored = []
    for s in db.scalars(select(MapSystem)):
        n = _squash(s.name)
        if n == q:
            rank = 0
        elif n.startswith(q):
            rank = 1
        elif q in n:
            rank = 2
        else:
            it = iter(n)
            if not all(ch in it for ch in q):
                continue
            rank = 3
        scored.append((rank, len(n), s))
    scored.sort(key=lambda x: (x[0], x[1], x[2].name))
    return [{"name": s.name, "region": s.region_name or "", "sec": round(s.security, 1),
             "cyno": can_cyno(s)} for _r, _l, s in scored[:limit]]
