"""Trip planning for bulk jobs: every JF starts at the home hub (AC2E-3), flies out empty
with all economizers, picks up at one or more pickup systems (refitting to ORE expanders
at the first dock), jumps loaded to the destination, refits economizers and flies home
empty.

How trips are built (greedy, in waves so all JFs fly together):
  - Each wave gives every pilot one trip, biggest hold first, while cargo remains.
  - A trip starts at the pickup with the most cargo left, loads what fits (bulky items
    whole first, finer stacks split to fill, never a tiny top-off), then tops up the
    hold from other pickups when that costs fewer extra jumps than a trip of its own.
  - Up to MAX_STOPS pickups per trip, visited in the order with the fewest jumps (then
    least LY).
Fuel: empty legs at the all-economizer rate, loaded legs at the ORE-expander rate, both
stacking-penalized like in game (see jump.economizer_factor).
"""
import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field
from typing import Callable

from app.services.jump import Hauler, fuel_for

MAX_STOPS = 4
SCRAP_M3 = 1000          # don't split a stack into a top-off smaller than this
MIN_TOPUP = 5000         # don't detour for less free space than this


@dataclass
class Cargo:
    type_id: int
    name: str
    qty: int
    unit_m3: float
    unit_value: float = 0.0

    @property
    def m3(self) -> float:
        return self.qty * self.unit_m3


@dataclass
class PickupCargo:
    system_id: int
    system: str
    location_id: int | None
    location: str
    items: list[Cargo] = field(default_factory=list)

    @property
    def m3(self) -> float:
        return sum(c.m3 for c in self.items)


@dataclass
class Hop:
    """A route between two systems: jumps and the LY of each jump, mids named."""
    jumps: int
    ly: list[float]
    mids: list[str]

    @property
    def total_ly(self) -> float:
        return sum(self.ly)


RouteFn = Callable[[int, int], "Hop | None"]


def _load_from(p: PickupCargo, free: float) -> list[dict]:
    """Take what fits from a pickup: bulky-per-unit first, kept whole where possible;
    finer stacks split to fill, but never a scrap that doesn't finish the stack."""
    taken = []
    for c in sorted(p.items, key=lambda c: (-c.unit_m3, -c.m3)):
        if c.qty <= 0 or free <= 0:
            continue
        if c.unit_m3 <= 0:
            n = c.qty
        else:
            n = min(c.qty, int((free + 1e-6) // c.unit_m3))
            if n <= 0 or (n < c.qty and n * c.unit_m3 < SCRAP_M3):
                continue
        c.qty -= n
        free -= n * c.unit_m3
        taken.append({"type_id": c.type_id, "name": c.name, "qty": n,
                      "m3": round(n * c.unit_m3, 2), "value": round(n * c.unit_value, 2)})
    p.items = [c for c in p.items if c.qty > 0]
    return taken


def _order(stops: list[PickupCargo], base: int, dest: int, route: RouteFn) -> tuple[list, int, float]:
    """Visit order with the fewest jumps (then least LY): base -> stops... -> dest."""
    best = None
    for perm in itertools.permutations(stops):
        path = [base] + [s.system_id for s in perm] + [dest]
        jumps, ly, ok = 0, 0.0, True
        for a, b in zip(path, path[1:]):
            h = route(a, b)
            if h is None:
                ok = False
                break
            jumps += h.jumps
            ly += h.total_ly
        if ok and (best is None or (jumps, ly) < best[1:]):
            best = (list(perm), jumps, ly)
    return best if best else (list(stops), 10 ** 6, 0.0)


def plan(pickups: list[PickupCargo], haulers: list[Hauler], base: tuple[int, str],
         dest: tuple[int, str], route: RouteFn, max_waves: int = 200) -> dict:
    """Plan trips for the remaining cargo. Mutates `pickups` (cargo gets taken)."""
    base_id, base_name = base
    dest_id, dest_name = dest
    out = {"base": base_name, "dest": dest_name, "trips": [], "too_big": [], "unreachable": []}

    # Pickups we can't reach, and items bigger than any hold, are reported, not planned.
    reachable = []
    for p in pickups:
        if route(base_id, p.system_id) is None or route(p.system_id, dest_id) is None:
            out["unreachable"].append(p.system)
        else:
            reachable.append(p)
    biggest = max((h.capacity for h in haulers), default=0)
    for p in reachable:
        for c in list(p.items):
            if c.unit_m3 > biggest:
                out["too_big"].append(f"{c.name} at {p.system}")
                p.items.remove(c)
    haulers = sorted([h for h in haulers if h.capacity > 0], key=lambda h: -h.capacity)
    if not haulers:
        return out

    def standalone(p: PickupCargo) -> int:
        a, b = route(base_id, p.system_id), route(p.system_id, dest_id)
        return (a.jumps if a else 99) + (b.jumps if b else 99)

    n = 0
    for wave in range(1, max_waves + 1):
        if not any(p.items for p in reachable):
            break
        for h in haulers:
            left = [p for p in reachable if p.items]
            if not left:
                break
            cap = h.capacity
            first = max(left, key=lambda p: p.m3)
            loads = {first.system_id: _load_from(first, cap)}
            used = sum(i["m3"] for i in loads[first.system_id])
            stops = [first]
            # Top up from other pickups while it's worth the detour.
            while cap - used > max(MIN_TOPUP, cap * 0.01) and len(stops) < MAX_STOPS:
                cands = [p for p in reachable if p.items and p not in stops]
                if not cands:
                    break
                _o, cur_jumps, _ = _order(stops, base_id, dest_id, route)
                scored = []
                for c in cands:
                    _o2, j2, ly2 = _order(stops + [c], base_id, dest_id, route)
                    extra = j2 - cur_jumps
                    if extra < standalone(c):
                        scored.append((extra, ly2, -c.m3, c))
                if not scored:
                    break
                scored.sort(key=lambda t: t[:3])
                pick = scored[0][3]
                got = _load_from(pick, cap - used)
                if not got:
                    break
                loads[pick.system_id] = got
                used += sum(i["m3"] for i in got)
                stops.append(pick)
            if used <= 0:
                continue
            n += 1
            out["trips"].append(_trip(n, wave, h, stops, loads, base, dest, route))
    out["totals"] = _totals(out["trips"])
    return out


def _trip(n: int, wave: int, h: Hauler, stops: list[PickupCargo], loads: dict, base, dest,
          route: RouteFn) -> dict:
    base_id, base_name = base
    dest_id, dest_name = dest
    ordered, _j, _ly = _order(stops, base_id, dest_id, route)
    p = h.profile
    path = [(base_id, base_name)] + [(s.system_id, s.system) for s in ordered] + [(dest_id, dest_name)]
    legs = []
    for i, ((a, an), (b, bn)) in enumerate(zip(path, path[1:])):
        fit = "empty" if i == 0 else "loaded"   # refit to expanders at the first pickup
        legs.append(_leg(route(a, b), an, bn, fit, h))
    if dest_id != base_id:
        legs.append(_leg(route(dest_id, base_id), dest_name, base_name, "empty", h))
    stop_rows = []
    for s in ordered:
        # One line per item type, even when it came from several contracts.
        merged: dict[int, dict] = {}
        for i in loads.get(s.system_id, []):
            m = merged.setdefault(i["type_id"], dict(i, qty=0, m3=0.0, value=0.0))
            m["qty"] += i["qty"]
            m["m3"] = round(m["m3"] + i["m3"], 2)
            m["value"] = round(m["value"] + i["value"], 2)
        items = sorted(merged.values(), key=lambda i: -i["m3"])
        stop_rows.append({"system": s.system, "system_id": s.system_id, "location": s.location,
                          "location_id": s.location_id, "items": items,
                          "m3": round(sum(i["m3"] for i in items), 2),
                          "value": round(sum(i["value"] for i in items), 2)})
    return {"id": f"t{n}", "wave": wave, "pilot": p.name, "pilot_id": p.character_id,
            "ship": p.ship_name or "", "fuel_type": p.fuel_type, "fuel_name": p.fuel_name,
            "capacity": round(h.capacity), "stops": stop_rows, "legs": legs,
            "m3": round(sum(s["m3"] for s in stop_rows), 2),
            "value": round(sum(s["value"] for s in stop_rows), 2),
            "jumps": sum(lg["jumps"] for lg in legs), "fuel": sum(lg["fuel"] for lg in legs),
            "ly": round(sum(lg["ly"] for lg in legs), 3)}


def _leg(hop: Hop | None, a: str, b: str, fit: str, h: Hauler) -> dict:
    per_ly = h.fuel_empty if fit == "empty" else h.fuel_loaded
    if hop is None:
        return {"from": a, "to": b, "fit": fit, "jumps": 0, "ly": 0.0, "fuel": 0, "mids": [],
                "error": "no route"}
    return {"from": a, "to": b, "fit": fit, "jumps": hop.jumps, "ly": round(hop.total_ly, 3),
            "fuel": sum(fuel_for(ly, per_ly) for ly in hop.ly), "mids": hop.mids}


def _totals(trips: list[dict]) -> dict:
    fuel: dict[str, int] = {}
    for t in trips:
        key = str(t.get("fuel_type") or "")
        fuel[key] = fuel.get(key, 0) + t["fuel"]
    return {"trips": len(trips), "waves": max((t["wave"] for t in trips), default=0),
            "jumps": sum(t["jumps"] for t in trips), "m3": round(sum(t["m3"] for t in trips), 2),
            "value": round(sum(t["value"] for t in trips), 2), "fuel_by_type": fuel,
            "peak_value": max((t["value"] for t in trips), default=0)}


# ------------------------------------------------------------------ steps

def steps(trip: dict) -> list[dict]:
    """The checklist for one trip, in order. Legs are base -> stop 1 -> ... -> stop k ->
    destination (-> base); after leg i you're at stop i+1, after leg k at the destination.
    Step ids are stable for a saved plan."""
    out = []
    tid, stops, has_return = trip["id"], trip["stops"], len(trip["legs"]) > len(trip["stops"]) + 1
    for i, lg in enumerate(trip["legs"]):
        out.append({"id": f"{tid}:j{i}", "kind": "jump", "leg": lg})
        if i < len(stops):
            out.append({"id": f"{tid}:l{i}", "kind": "load", "stop": stops[i], "refit": i == 0})
        elif i == len(stops):
            out.append({"id": f"{tid}:d", "kind": "deliver", "at": lg["to"],
                        "refit": has_return})
    return out


def fingerprint(contract_ids: list[int], dest_id: int | None, pilot_ids: list[int]) -> str:
    raw = json.dumps([sorted(contract_ids), dest_id, sorted(pilot_ids)])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def loaded_cargo(trips: list[dict]) -> dict[tuple[int, int], int]:
    """(system_id, type_id) -> quantity loaded by these trips."""
    out: dict[tuple[int, int], int] = {}
    for t in trips:
        for s in t["stops"]:
            for i in s["items"]:
                k = (s["system_id"], i["type_id"])
                out[k] = out.get(k, 0) + i["qty"]
    return out


def ceil_div(a: float, b: float) -> int:
    return math.ceil(a / b) if b else 0


# ------------------------------------------------------------------ from the database

VOLUMES_KEY = "bulk:volumes"


async def fill_unit_volumes(type_ids: set[int]) -> dict[int, float]:
    """Packaged m3 per unit by type id (ESI /universe/types, cached in settings)."""
    from app.db import session_scope
    from app.esi import client as esi
    from app.services.jump import _get_json, _set_json
    with session_scope() as db:
        cache = {int(k): v for k, v in _get_json(db, VOLUMES_KEY, {}).items()}
    for tid in sorted(type_ids - set(cache)):
        try:
            info = await esi.get(f"/universe/types/{tid}")
        except Exception:  # noqa: BLE001 — unknown volume: falls back to the contract's
            continue
        cache[tid] = float(info.get("packaged_volume") or info.get("volume") or 0)
    with session_scope() as db:
        _set_json(db, VOLUMES_KEY, {str(k): v for k, v in cache.items()})
        db.commit()
    return cache


def _route_fn(db, profile):
    """Memoized jumps between two systems for this JF (straight jump route; mids named)."""
    from app.services.jump import plan_route
    memo: dict[tuple[int, int], Hop | None] = {}

    def route(a: int, b: int) -> Hop | None:
        if a == b:
            return Hop(0, [], [])
        if (a, b) not in memo:
            r = plan_route(db, a, b, profile)
            memo[(a, b)] = None if r.error or (not r.legs and a != b) else Hop(
                len(r.legs), [lg.ly for lg in r.legs], [lg.to.name for lg in r.legs[:-1]])
        return memo[(a, b)]
    return route


def job_cargo(db, job, volumes: dict[int, float]) -> list[PickupCargo]:
    """The job's live contracts as cargo per pickup system. A type with no known volume
    takes its share of the contract's m3."""
    from app.models import MapSystem
    by_sys: dict[int, PickupCargo] = {}
    for c in job.live:
        loc = c.start_location
        if not loc or not loc.system_id:
            continue
        p = by_sys.get(loc.system_id)
        if p is None:
            ms = db.get(MapSystem, loc.system_id)
            p = by_sys[loc.system_id] = PickupCargo(loc.system_id, ms.name if ms else (loc.system_name or "?"),
                                                    c.start_location_id, loc.display)
        items = [it for it in c.items if it.is_included]
        known = sum(volumes.get(it.type_id, 0) * it.quantity for it in items if it.type_id in volumes)
        unknown_qty = sum(it.quantity for it in items if it.type_id not in volumes)
        per_unknown = max(0.0, ((c.volume_m3 or 0) - known) / unknown_qty) if unknown_qty else 0.0
        for it in items:
            unit = volumes.get(it.type_id, per_unknown)
            merged = next((x for x in p.items if x.type_id == it.type_id and x.unit_m3 == unit), None)
            if merged:
                merged.qty += it.quantity
            else:
                from app.services.assets import type_names
                name = type_names(db, {it.type_id}).get(it.type_id, f"Type {it.type_id}")
                p.items.append(Cargo(it.type_id, name + (" (Copy)" if it.is_bpc else ""),
                                     it.quantity, unit, it.unit_value or 0.0))
    return list(by_sys.values())


async def make_plan(job_id: int) -> dict:
    """(Re-)plan a job and save it. Trips you've already started (any step ticked) are
    kept as they are; only the cargo they don't cover is planned again."""
    from app.db import session_scope, utcnow
    from app.models import BulkJob, BulkPlan, MapSystem
    from app.services import bulk, jump
    with session_scope() as db:
        job = db.get(BulkJob, job_id)
        type_ids = {it.type_id for c in job.live for it in c.items}
    volumes = await fill_unit_volumes(type_ids)
    with session_scope() as db:
        job = db.get(BulkJob, job_id)
        hubs = bulk.hub_systems(db)
        if not hubs:
            return {"error": "Download the star map on the Jump Planner first."}
        base = hubs[0]
        dest = db.get(MapSystem, job.dest_system_id) if job.dest_system_id else bulk.nearest_hub(db, job)
        dest = dest or base
        profs = [p for p in jump.profiles(db, enabled_only=True) if p.ready]
        if not profs:
            return {"error": "No jump freighter pilot is set up (Jump Planner › Pilots & ships)."}
        haulers = [jump.Hauler(p) for p in profs]
        route = _route_fn(db, min(profs, key=lambda p: p.range_ly))

        old = json.loads(job.plan.data) if job.plan else {}
        done = set(json.loads(job.plan.done)) if job.plan else set()
        kept = [t for t in old.get("trips", []) if any(s["id"] in done for s in steps(t))]
        cargo = job_cargo(db, job, volumes)
        taken = loaded_cargo(kept)
        for p in cargo:
            for c in p.items:
                k = (p.system_id, c.type_id)
                use = min(c.qty, taken.get(k, 0))
                c.qty -= use
                taken[k] = taken.get(k, 0) - use
            p.items = [c for c in p.items if c.qty > 0]
        result = plan(cargo, haulers, (base.system_id, base.name), (dest.system_id, dest.name),
                      route)
        # New trips continue the numbering after the kept ones.
        start = max((int(t["id"][1:]) for t in kept), default=0)
        for i, t in enumerate(result["trips"], 1):
            t["id"] = f"t{start + i}"
        for t in kept:
            t["kept"] = True
        result["trips"] = kept + result["trips"]
        result["totals"] = _totals(result["trips"])
        result["planned_at"] = utcnow().isoformat()
        result["dest_id"] = dest.system_id
        fp = fingerprint([c.contract_id for c in job.live], dest.system_id,
                         [p.character_id for p in profs])
        row = job.plan or BulkPlan(job_id=job.id, created_at=utcnow())
        row.data, row.fingerprint, row.updated_at = json.dumps(result), fp, utcnow()
        row.done = json.dumps(sorted(done & {s["id"] for t in result["trips"] for s in steps(t)}))
        if job.plan is None:
            db.add(row)
        if job.status in ("new", "accepted"):
            job.status = "hauling" if done else job.status
        db.commit()
        return result


def current_fingerprint(db, job) -> str:
    from app.models import MapSystem
    from app.services import bulk, jump
    dest = job.dest_system_id or (bulk.nearest_hub(db, job).system_id
                                  if bulk.nearest_hub(db, job) else None)
    profs = [p for p in jump.profiles(db, enabled_only=True) if p.ready]
    return fingerprint([c.contract_id for c in job.live], dest, [p.character_id for p in profs])


# ------------------------------------------------------------------ costs and pricing

PRICING_KEYS = {"isk_per_jump": ("bulk:isk_per_jump", 0.0), "risk_pct": ("bulk:risk_pct", 0.0)}
LADDER = (15, 25, 50, 100)


def pricing_settings(db) -> dict[str, float]:
    from app.models import Setting
    out = {}
    for k, (key, default) in PRICING_KEYS.items():
        row = db.get(Setting, key)
        try:
            out[k] = float(row.value) if row else default
        except ValueError:
            out[k] = default
    return out


def save_pricing(db, isk_per_jump: float, risk_pct: float) -> None:
    from app.models import Setting
    for k, v in (("isk_per_jump", max(0.0, isk_per_jump)), ("risk_pct", max(0.0, min(100.0, risk_pct)))):
        key = PRICING_KEYS[k][0]
        row = db.get(Setting, key) or Setting(key=key, value="0")
        row.value = str(v)
        db.merge(row)


def costs(db, job, data: dict) -> dict:
    """What hauling this job costs (fuel at Jita buy, your ISK per jump, a risk share of
    the cargo value) and suggested rewards at +15/25/50/100%."""
    from app.services import prices
    from app.services.jump import ISOTOPES
    t = data.get("totals") or _totals(data.get("trips", []))
    fuel = {int(k): v for k, v in t.get("fuel_by_type", {}).items() if k}
    price = prices.jita_buy(db, set(fuel))
    fuel_isk = sum(v * price.get(k, 0) for k, v in fuel.items())
    s = pricing_settings(db)
    jumps = t.get("jumps", 0)
    time_isk = s["isk_per_jump"] * jumps
    risk_isk = s["risk_pct"] / 100 * (job.value_isk or 0)
    total = fuel_isk + time_isk + risk_isk
    m3 = job.m3 or 0
    ladder = [{"pct": pct, "reward": total * (1 + pct / 100),
               "per_jump": total * (1 + pct / 100) / jumps if jumps else 0,
               "per_m3": total * (1 + pct / 100) / m3 if m3 else 0} for pct in LADDER]
    return {"fuel": [(ISOTOPES.get(k, f"Type {k}"), v, v * price.get(k, 0)) for k, v in fuel.items()],
            "fuel_isk": fuel_isk, "fuel_priced": all(k in price for k in fuel),
            "time_isk": time_isk, "risk_isk": risk_isk, "total": total, "jumps": jumps,
            "ladder": ladder, **s}
