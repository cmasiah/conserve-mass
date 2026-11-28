"""Hauling › Jump Planner: routes, courier lanes, JF pilots, cyno points."""
import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.esi import client as esi
from app.models import Character, CynoPoint, JumpPilot, MapSystem
from app.security import require_csrf, require_user
from app.services import jump as svc
from app.web.templating import flash, render

router = APIRouter(prefix="/jump", dependencies=[Depends(require_user)])
_tasks: set = set()   # keep background imports alive


def _back(tab: str, **extra) -> RedirectResponse:
    q = "&".join(f"{k}={v}" for k, v in extra.items() if v not in (None, ""))
    return RedirectResponse(f"/jump?tab={tab}" + (f"&{q}" if q else ""), status_code=303)


@router.get("")
async def planner(request: Request, tab: str = "route", pilot: int = 0, frm: str = "",
                  to: str = "", cynos: int = 0, db: Session = Depends(get_db)):
    status = svc.map_status(db)
    profs = svc.profiles(db)
    ready = [p for p in profs if p.ready]
    chosen = next((p for p in ready if p.character_id == pilot), ready[0] if ready else None)
    ctx = {"tab": tab, "status": status, "profiles": profs, "ready": ready, "chosen": chosen,
           "frm": frm, "to": to, "cynos": cynos, "route": None, "lanes": [], "prices": {},
           "cyno_points": [], "bonus": svc.bonuses(db), "bonus_labels": svc.BONUS_LABELS,
           "now": utcnow(),
           "system_names": (db.scalars(select(MapSystem.name).order_by(MapSystem.name)).all()
                            if tab in ("route", "cynos") and status["systems"] else [])}
    if tab == "route" and frm and to and chosen and status["systems"]:
        a, b = svc.find_system(db, frm), svc.find_system(db, to)
        if a is None or b is None:
            flash(request, f"Unknown system: {to if a else frm}", "error")
        else:
            gates = None
            if not svc.can_cyno(b):
                cands = svc.landing_candidates(db, b, svc.graph(db, bool(cynos)))
                gates = await svc.gate_counts([c.system_id for c in cands], b.system_id)
            ctx["route"] = svc.plan_route(db, a.system_id, b.system_id, chosen, bool(cynos), gates)
            ctx["frm"], ctx["to"] = a.name, b.name
    if tab == "lanes" and status["systems"]:
        ctx["lanes"] = svc.lanes(db, chosen, only_cynos=bool(cynos))
    if tab in ("route", "lanes") and chosen:
        ctx["prices"] = await svc.isotope_prices(db)
    if tab == "cynos":
        rows = db.execute(select(CynoPoint, MapSystem).join(
            MapSystem, MapSystem.system_id == CynoPoint.system_id, isouter=True)).all()
        ctx["cyno_points"] = sorted(rows, key=lambda r: (r[1].name if r[1] else ""))
    if tab == "pilots":
        have = {p.character_id for p in profs}
        ctx["candidates"] = [c for c in db.scalars(select(Character).order_by(Character.name))
                             if c.character_id not in have]
        ctx["jf_types"] = svc._get_json(db, "jump:jf_types", {})
        ctx["pilot_rows"] = {jp.character_id: jp for jp in db.scalars(select(JumpPilot))}
    return render(request, "jump.html", **ctx)


@router.post("/map/import")
async def map_import(request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    if svc.map_status(db).get("state") == "running":
        flash(request, "The star map is already downloading.")
    else:
        t = asyncio.get_running_loop().create_task(svc.import_map())
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        flash(request, "Downloading the star map (one time, a few minutes). Refresh to see progress.")
    return _back("route")


@router.post("/pilots/add")
async def add_pilot(request: Request, csrf: str = Form(""), character_id: int = Form(0),
                    db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    char = db.get(Character, character_id)
    if char and db.get(JumpPilot, character_id) is None:
        db.add(JumpPilot(character_id=character_id))
        db.commit()
        await refresh_ships(db)
        flash(request, f"{char.name} added as a JF pilot.")
    return _back("pilots")


async def refresh_ships(db: Session) -> None:
    """Look up the four JF hulls by name, and load base stats for every ship in use."""
    types = svc._get_json(db, "jump:jf_types", {})
    if len(types) < 4:
        try:
            res = await esi.post("/universe/ids", list(svc.RACE))
            types = {t["name"]: t["id"] for t in res.get("inventory_types", [])}
            svc._set_json(db, "jump:jf_types", types)
            db.commit()
        except esi.EsiError:
            pass
    wanted = set(types.values())
    for p in svc.profiles(db):
        if p.ship_type_id:
            wanted.add(p.ship_type_id)
    for t in wanted:
        await svc.ship_base(db, t)


@router.post("/pilots/refresh")
async def refresh(request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    await refresh_ships(db)
    flash(request, "Ship data refreshed.")
    return _back("pilots")


def _num(v: str) -> float | None:
    v = (v or "").replace(",", "").strip().lower()
    mult = 1
    if v[-1:] in ("k", "m", "b"):
        mult = {"k": 1e3, "m": 1e6, "b": 1e9}[v[-1]]
        v = v[:-1]
    try:
        return float(v) * mult if v else None
    except ValueError:
        return None


@router.post("/pilots/{character_id}")
async def save_pilot(request: Request, character_id: int, db: Session = Depends(get_db)):
    form = await request.form()
    require_csrf(request, str(form.get("csrf", "")))
    jp = db.get(JumpPilot, character_id)
    if jp is None:
        return _back("pilots")
    if form.get("remove"):
        db.delete(jp)
        db.commit()
        flash(request, "Removed from JF pilots.")
        return _back("pilots")
    ship = str(form.get("ship_type_id", ""))
    jp.ship_type_id = int(ship) if ship.isdigit() else None
    fit = str(form.get("fit", "auto"))
    if fit == "auto":
        jp.economizers = jp.expanders = None
    else:
        econ = int(fit.split(":")[0]) if fit[0].isdigit() else 0
        jp.economizers, jp.expanders = econ, 3 - econ
    jp.range_override = _num(str(form.get("range_override", "")))
    jp.fuel_override = _num(str(form.get("fuel_override", "")))
    jp.cargo_override = _num(str(form.get("cargo_override", "")))
    jp.max_collateral = _num(str(form.get("max_collateral", "")))
    jp.enabled = bool(form.get("enabled"))
    jp.updated_at = utcnow()
    db.commit()
    if jp.ship_type_id:
        await svc.ship_base(db, jp.ship_type_id)
    flash(request, "Pilot saved.")
    return _back("pilots")


@router.post("/bonuses")
async def save_bonuses(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    require_csrf(request, str(form.get("csrf", "")))
    values = {}
    for k in svc.BONUS_DEFAULTS:
        v = _num(str(form.get(k, "")))
        if v is not None:
            values[k] = v / 100 if v > 1 else v   # accept 20 or 0.20
    svc.save_bonuses(db, values)
    db.commit()
    flash(request, "Bonus values saved.")
    return _back("pilots")


@router.post("/cynos/add")
def add_cyno(request: Request, csrf: str = Form(""), system: str = Form(""), label: str = Form(""),
             db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    s = svc.find_system(db, system)
    if s is None:
        flash(request, f"Unknown system: {system}", "error")
    elif not svc.can_cyno(s):
        flash(request, f"{s.name} is high-sec (or Pochven): cynos can't be lit there.", "error")
    else:
        row = db.get(CynoPoint, s.system_id) or CynoPoint(system_id=s.system_id)
        row.label = label[:120]
        db.add(row)
        db.commit()
        svc._GRAPHS.clear()
        flash(request, f"Cyno point added: {s.name}.")
    return _back("cynos")


@router.post("/cynos/{system_id}/delete")
def delete_cyno(request: Request, system_id: int, csrf: str = Form(""), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    row = db.get(CynoPoint, system_id)
    if row:
        db.delete(row)
        db.commit()
        svc._GRAPHS.clear()
    return _back("cynos")
