"""Industry › Assets and Industry › Materials."""
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Request
import json

from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import (
    Asset, AssetNode, AssetWatch, Character, Corporation, Location, MaterialTarget, Setting,
    TypeInfo,
)
from app.security import require_csrf, require_user
from app.services import assets as svc
from app.services.assets import material_lines
from app.services import industry, sync_info
from app.services.places import is_system
from app.web.templating import flash, render

router = APIRouter(prefix="/industry", dependencies=[Depends(require_user)])
MAX_LINES = 1500


# ------------------------------------------------------------------ shared lookups

def _context(db: Session) -> dict:
    corps = {c.corporation_id: c for c in db.scalars(select(Corporation))}
    return {
        "locations": {loc.location_id: loc for loc in db.scalars(select(Location))},
        "pilots": {c.character_id: c.name for c in db.scalars(select(Character))},
        "corps": corps,
        "divs": {cid: industry.division_names(db, cid) for cid in corps},
        "containers": {n.item_id: n for n in db.scalars(select(AssetNode))},
    }


def site_label(ctx: dict, site_id: int) -> tuple[str, str]:
    """(place name, system name)"""
    loc = ctx["locations"].get(site_id)
    if loc and loc.name:
        return loc.name, loc.system_name or ""
    if is_system(site_id):
        return "In space", ""
    return f"Unknown structure {site_id}", (loc.system_name if loc else "") or ""


def owner_label(ctx: dict, owner_type: str, owner_id: int, division: str) -> str:
    if owner_type == "character":
        return f"{ctx['pilots'].get(owner_id, owner_id)} (personal)"
    corp = ctx["corps"].get(owner_id)
    tick = f"[{corp.ticker}] " if corp and corp.ticker else ""
    if not division:
        return f"{tick}Corp (outside hangars)"
    return f"{tick}{ctx['divs'].get(owner_id, {}).get(division, division)}"


def container_label(ctx: dict, item_id: int | None) -> str:
    if not item_id:
        return ""
    n = ctx["containers"].get(item_id)
    return (n.name if n and n.name else "Container") if n else "Container"


# Item kinds for the type filter: ESI category ids, with a name fallback for types whose
# category hasn't been looked up yet.
KINDS = {
    "bp": ("Blueprints", {9}, (" Blueprint",)),
    "skin": ("SKINs", {91}, (" SKIN",)),
    "ship": ("Ships", {6}, ()),
    "module": ("Modules", {7}, ()),
    "material": ("Materials", {4, 25, 43}, ()),
}


def _who_filter(stmt, who: str):
    """who: '' everyone, 'corp', 'personal', or 'char:<id>'."""
    if who == "corp":
        return stmt.where(Asset.owner_type == "corporation")
    if who == "personal":
        return stmt.where(Asset.owner_type == "character")
    if who.startswith("char:") and who[5:].isdigit():
        return stmt.where(Asset.owner_type == "character", Asset.owner_id == int(who[5:]))
    return stmt


def _filtered(db: Session, q: str, site: int, owner: str, division: str, container: int,
              watched: bool, who: str = "", kind: str = ""):
    # Plain columns, not full ORM objects: ~3x faster on 100k stacks.
    stmt = select(Asset.type_id, Asset.quantity, Asset.site_id, Asset.is_bpc)
    if watched:
        stmt = stmt.where(svc.watch_filter(db))
    if site:
        stmt = stmt.where(Asset.site_id == site)
    if owner:
        otype, _, oid = owner.partition(":")
        if oid.isdigit():
            stmt = stmt.where(Asset.owner_type == otype, Asset.owner_id == int(oid))
    if division:
        stmt = stmt.where(Asset.division == division)
    if container:
        stmt = stmt.where(Asset.container_path.like(f"%,{container},%"))
    stmt = _who_filter(stmt, who)
    rows = db.execute(stmt).all()
    names = svc.type_names(db, {a.type_id for a in rows})
    if kind in KINDS:
        _label, cats, suffixes = KINDS[kind]
        cat_of = {t.type_id: t.category_id for t in db.scalars(
            select(TypeInfo).where(TypeInfo.type_id.in_({a.type_id for a in rows})))}

        def is_kind(tid):
            c = cat_of.get(tid)
            if c is not None:
                return c in cats
            return any(names.get(tid, "").endswith(sfx) for sfx in suffixes)
        rows = [a for a in rows if is_kind(a.type_id)]
    if q:
        needle = q.lower()
        rows = [a for a in rows if needle in names.get(a.type_id, "").lower()]
    return rows, names


def _aggregate(rows, names, ctx) -> list[svc.Line]:
    """One line per item type; blueprint copies get their own line, like in game."""
    lines: dict[tuple[int, bool], svc.Line] = {}
    for a in rows:
        key = (a.type_id, bool(a.is_bpc))
        ln = lines.get(key)
        if ln is None:
            name = names.get(a.type_id, f"Type {a.type_id}")
            ln = lines[key] = svc.Line(a.type_id, name + (" (Copy)" if a.is_bpc else ""))
        ln.quantity += a.quantity
        ln.stacks += 1
        where = site_label(ctx, a.site_id)[0]
        ln.places[where] = ln.places.get(where, 0) + a.quantity
    return sorted(lines.values(), key=lambda x: x.name.lower())


PINS_KEY = "assets:pinned_systems"


def pinned(db: Session) -> list[str]:
    row = db.get(Setting, PINS_KEY)
    try:
        return list(json.loads(row.value)) if row else []
    except ValueError:
        return []


# ------------------------------------------------------------------ places tree

def places_tree(db: Session, ctx: dict, who: str = "") -> list[dict]:
    """system -> site -> owner/division -> containers, with item counts and watch state."""
    watches = db.scalars(select(AssetWatch)).all()
    w_sys = {w.target_id for w in watches if w.kind == "system"}
    w_site = {w.target_id for w in watches if w.kind == "site"}
    w_div = {(w.target_id, w.owner_id, w.division) for w in watches if w.kind == "division"}
    w_pers = {(w.target_id, w.owner_id) for w in watches if w.kind == "personal"}
    w_cont = {w.target_id for w in watches if w.kind == "container"}

    groups = db.execute(_who_filter(select(
        Asset.site_id, Asset.owner_type, Asset.owner_id, Asset.division, Asset.container_id,
        func.count(), func.sum(Asset.quantity)), who).group_by(
        Asset.site_id, Asset.owner_type, Asset.owner_id, Asset.division,
        Asset.container_id)).all()
    systems: dict[str, dict] = {}
    for site_id, otype, oid, div, cont, n, _qty in groups:
        name, sysname = site_label(ctx, site_id)
        loc = ctx["locations"].get(site_id)
        sys_key = sysname or "Unknown system"
        s = systems.setdefault(sys_key, {"name": sys_key, "system_id": loc.system_id if loc else None,
                                         "sites": {}, "count": 0})
        s["watched"] = bool(s.get("system_id")) and s["system_id"] in w_sys
        site = s["sites"].setdefault(site_id, {"id": site_id, "name": name, "owners": {},
                                               "count": 0, "watched": site_id in w_site})
        okey = (otype, oid, div if otype == "corporation" else "")
        owner = site["owners"].setdefault(okey, {
            "type": otype, "id": oid, "division": okey[2],
            "label": owner_label(ctx, otype, oid, okey[2]), "containers": {}, "count": 0,
            "watched": ((site_id, oid, okey[2]) in w_div) if otype == "corporation"
            else ((site_id, oid) in w_pers)})
        if cont:
            c = owner["containers"].setdefault(cont, {"id": cont, "label": container_label(ctx, cont),
                                                      "count": 0, "watched": cont in w_cont})
            c["count"] += n
        owner["count"] += n
        site["count"] += n
        s["count"] += n
    pins = pinned(db)
    for s in systems.values():
        s["pinned"] = s["name"] in pins
    # Pinned systems first (in the order pinned), then the busiest.
    out = sorted(systems.values(), key=lambda s: (
        pins.index(s["name"]) if s["pinned"] else len(pins), -s["count"], s["name"]))
    for s in out:
        s["sites"] = sorted(s["sites"].values(), key=lambda x: (-x["count"], x["name"]))
        for site in s["sites"]:
            site["owners"] = sorted(site["owners"].values(), key=lambda o: (o["type"], o["label"]))
            for o in site["owners"]:
                o["containers"] = sorted(o["containers"].values(), key=lambda c: c["label"])
    return out


# ------------------------------------------------------------------ Assets page

def _owners(db: Session, ctx: dict) -> list[tuple[int, str]]:
    """Characters that own synced assets, for the owner filter."""
    ids = db.scalars(select(Asset.owner_id).where(Asset.owner_type == "character").distinct())
    return sorted(((i, ctx["pilots"].get(i, str(i))) for i in ids), key=lambda x: x[1].lower())


@router.get("/assets")
def assets_page(request: Request, q: str = "", site: int = 0, owner: str = "",
                division: str = "", container: int = 0, watched: int = 0, who: str = "",
                kind: str = "", db: Session = Depends(get_db)):
    ctx = _context(db)
    kind = kind if kind in KINDS else ""
    rows, names = _filtered(db, q, site, owner, division, container, bool(watched), who, kind)
    lines = _aggregate(rows, names, ctx)
    total_stacks = db.scalar(select(func.count()).select_from(Asset)) or 0
    scope = []
    if who:
        scope.append({"corp": "Corp hangars", "personal": "Personal"}.get(
            who, ctx["pilots"].get(int(who[5:]), who) if who[5:].isdigit() else who))
    if kind:
        scope.append(KINDS[kind][0])
    if watched:
        scope.append("watched places")
    if site:
        scope.append(site_label(ctx, site)[0])
    if owner:
        otype, _, oid = owner.partition(":")
        scope.append(owner_label(ctx, otype, int(oid), division) if oid.isdigit() else owner)
    if container:
        scope.append(container_label(ctx, container))
    return render(request, "industry_assets.html", lines=lines[:MAX_LINES],
                  more=max(0, len(lines) - MAX_LINES), shown_stacks=len(rows),
                  total_stacks=total_stacks, tree=places_tree(db, ctx, who), q=q, site=site,
                  owner=owner, division=division, container=container, watched=watched,
                  who=who, kind=kind, kinds=KINDS, owners=_owners(db, ctx),
                  scope=scope, clip=svc.clipboard_text([(ln.name, ln.quantity) for ln in lines]),
                  watch_count=db.scalar(select(func.count()).select_from(AssetWatch)) or 0,
                  now=utcnow(), sync_feeds=sync_info.feeds(db, "industry"))


@router.get("/assets/export.txt", response_class=PlainTextResponse)
def assets_export(q: str = "", site: int = 0, owner: str = "", division: str = "",
                  container: int = 0, watched: int = 0, who: str = "", kind: str = "",
                  db: Session = Depends(get_db)):
    """The same selection as the page, as `Name Quantity` lines (for other tools)."""
    rows, names = _filtered(db, q, site, owner, division, container, bool(watched), who,
                            kind if kind in KINDS else "")
    return svc.clipboard_text([(ln.name, ln.quantity) for ln in _aggregate(rows, names, _context(db))])


def _quiet(request: Request) -> bool:
    """A background (fetch) request from the page: answer with JSON, no flash, no reload."""
    return request.headers.get("x-requested-with") == "fetch"


@router.post("/assets/watch")
def watch(request: Request, csrf: str = Form(""), kind: str = Form(""), target: int = Form(0),
          owner_id: int = Form(0), division: str = Form(""), label: str = Form(""),
          back: str = Form("/industry/assets"), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    if kind not in ("system", "site", "division", "personal", "container") or not target:
        if _quiet(request):
            return JSONResponse({"ok": False, "error": "Unknown place."}, status_code=400)
        flash(request, "Unknown place.", "error")
    else:
        on = svc.toggle_watch(db, kind, target, owner_id or None, division, label)
        db.commit()
        if _quiet(request):
            return JSONResponse({"ok": True, "on": on})
        flash(request, f"{'Watching' if on else 'Stopped watching'} {label or 'that place'}."
                       + (" It now counts toward Materials." if on else ""))
    return RedirectResponse(back if back.startswith("/industry/") else "/industry/assets",
                            status_code=303)


@router.post("/assets/pin")
def pin(request: Request, csrf: str = Form(""), system: str = Form(""),
        back: str = Form("/industry/assets"), db: Session = Depends(get_db)):
    """Pin a system to the top of the places list (or unpin it)."""
    require_csrf(request, csrf)
    pins = pinned(db)
    on = system not in pins
    pins = pins + [system] if on else [p for p in pins if p != system]
    row = db.get(Setting, PINS_KEY) or Setting(key=PINS_KEY, value="[]")
    row.value = json.dumps(pins)
    db.merge(row)
    db.commit()
    if _quiet(request):
        return JSONResponse({"ok": True, "on": on})
    return RedirectResponse(back if back.startswith("/industry/") else "/industry/assets",
                            status_code=303)


# ------------------------------------------------------------------ Materials page

@router.get("/materials")
def materials_page(request: Request, scope: str = "watched", db: Session = Depends(get_db)):
    sections, every = material_lines(db, scope)
    shopping = [(ln.name, ln.short) for ln in every if ln.short]
    selling = [(ln.name, ln.excess) for ln in every if ln.excess]
    return render(request, "industry_materials.html", sections=sections, scope=scope,
                  shopping=sorted(shopping), selling=sorted(selling),
                  clip_all=svc.clipboard_text([(ln.name, ln.quantity) for ln in sorted(
                      every, key=lambda x: x.name.lower())]),
                  clip_shop=svc.clipboard_text(sorted(shopping)),
                  clip_sell=svc.clipboard_text(sorted(selling)),
                  watch_count=db.scalar(select(func.count()).select_from(AssetWatch)) or 0,
                  now=utcnow(), sync_feeds=sync_info.feeds(db, "industry"))


@router.get("/materials/export.txt", response_class=PlainTextResponse)
def materials_export(list: str = "all", scope: str = "watched", db: Session = Depends(get_db)):
    _sections, every = material_lines(db, scope)
    if list == "shopping":
        pairs = [(ln.name, ln.short) for ln in every]
    elif list == "sell":
        pairs = [(ln.name, ln.excess) for ln in every]
    else:
        pairs = [(ln.name, ln.quantity) for ln in every]
    return svc.clipboard_text(sorted(pairs))


def _qty(v: str) -> int | None:
    v = (v or "").replace(",", "").replace(" ", "").strip().lower()
    if not v:
        return None
    mult = 1
    if v[-1:] in ("k", "m", "b"):
        mult = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[v[-1]]
        v = v[:-1]
    try:
        return max(0, int(float(v) * mult))
    except ValueError:
        return None


@router.post("/materials/targets")
async def save_targets(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    require_csrf(request, str(form.get("csrf", "")))
    changed = 0
    ids = {int(k.split("_", 1)[1]) for k in form if k.startswith(("min_", "max_"))
           and k.split("_", 1)[1].isdigit()}
    for tid in ids:
        lo, hi = _qty(str(form.get(f"min_{tid}", ""))), _qty(str(form.get(f"max_{tid}", "")))
        row = db.get(MaterialTarget, tid)
        if lo is None and hi is None:
            if row:
                db.delete(row)
                changed += 1
            continue
        row = row or MaterialTarget(type_id=tid)
        if (row.min_qty, row.max_qty) != (lo, hi):
            changed += 1
        row.min_qty, row.max_qty = lo, hi
        db.add(row)
    db.commit()
    flash(request, f"Saved stock levels ({changed} changed).")
    scope = str(form.get("scope", "watched"))
    return RedirectResponse(f"/industry/materials?scope={scope}", status_code=303)
