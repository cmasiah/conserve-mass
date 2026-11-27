"""Industry pages: slots & skills, job tracker, blueprint library."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.esi.sso import INDUSTRY_SCOPES
from app.models import Blueprint, Character, Corporation, EntityName, EsiToken, IndustryJob
from app.security import require_csrf, require_user
from app.services import industry
from app.web.templating import flash, render

router = APIRouter(prefix="/industry", dependencies=[Depends(require_user)])

CATS = ("manufacturing", "science", "reactions")


def _names(db: Session, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {e.entity_id: e.name for e in
            db.scalars(select(EntityName).where(EntityName.entity_id.in_(ids)))}


def _missing_scopes(db: Session) -> list[tuple[Character, list[str]]]:
    out = []
    for c in db.scalars(select(Character).order_by(Character.name)):
        tok = db.get(EsiToken, c.character_id)
        have = tok.scope_set if tok else set()
        missing = [s for s in INDUSTRY_SCOPES if s not in have]
        if missing:
            out.append((c, missing))
    return out


@router.get("/slots")
def slots(request: Request, sort: str = "name", only: str = "", db: Session = Depends(get_db)):
    rows = industry.character_overview(db)
    if only in CATS:
        rows = [r for r in rows if r.capacity.get(only, 0) >= 10]
    elif only == "capital":
        rows = [r for r in rows if r.capital_level >= 1]
    if sort in CATS:
        rows.sort(key=lambda r: (r.capacity.get(sort, 0), r.free(sort)), reverse=True)
    elif sort == "capital":
        rows.sort(key=lambda r: r.capital_level, reverse=True)
    totals = {c: (sum(r.capacity.get(c, 0) for r in rows),
                  sum(r.used.get(c, 0) for r in rows)) for c in CATS}
    return render(request, "industry_slots.html", rows=rows, sort=sort, only=only, cats=CATS,
                  totals=totals, max_slots=industry.MAX_SLOTS, missing=_missing_scopes(db),
                  now=utcnow())


@router.get("/jobs")
def jobs(request: Request, cat: str = "", who: int = 0, db: Session = Depends(get_db)):
    q = select(IndustryJob).order_by(IndustryJob.end_date)
    if who:
        q = q.where(IndustryJob.installer_id == who)
    rows = db.scalars(q).all()
    if cat in CATS:
        rows = [j for j in rows if industry.ACTIVITY_CATEGORY.get(j.activity_id) == cat]
    names = _names(db, {j.product_type_id or j.blueprint_type_id for j in rows}
                   | {j.installer_id for j in rows})
    corps = {c.corporation_id: c for c in db.scalars(select(Corporation))}
    installers = sorted({(j.installer_id, names.get(j.installer_id, str(j.installer_id)))
                         for j in db.scalars(select(IndustryJob))}, key=lambda x: x[1])
    now = utcnow()
    view = []
    for j in rows:
        total = (j.end_date - j.start_date).total_seconds() or 1
        pct = 100 if j.status == "ready" or j.end_date <= now else \
            max(0, min(100, (now - j.start_date).total_seconds() / total * 100))
        view.append({"j": j, "pct": pct, "done": j.end_date <= now or j.status == "ready",
                     "what": names.get(j.product_type_id or j.blueprint_type_id,
                                       f"Type {j.product_type_id or j.blueprint_type_id}"),
                     "who": names.get(j.installer_id, str(j.installer_id)),
                     "activity": industry.ACTIVITY_NAMES.get(j.activity_id,
                                                             f"Activity {j.activity_id}"),
                     "owner": (corps[j.owner_id].ticker or "Corp") if j.owner_type ==
                     "corporation" and j.owner_id in corps else "Personal"})
    ready = sum(1 for v in view if v["done"])
    return render(request, "industry_jobs.html", rows=view, cat=cat, who=who, cats=CATS,
                  installers=installers, ready=ready, now=now)


def aggregate_blueprints(items: list[dict]) -> list[dict]:
    """Merge identical blueprints into one line with a count.

    Identical = same type, original/copy, ME, TE and runs. A stacked BPO
    (quantity > 0) counts as that many blueprints.
    """
    merged: dict[tuple, dict] = {}
    for it in items:
        b = it["b"]
        key = (b.type_id, b.is_copy, b.material_efficiency, b.time_efficiency,
               b.runs if b.is_copy else -1)
        n = b.quantity if b.quantity > 0 else 1
        if key in merged:
            merged[key]["count"] += n
        else:
            merged[key] = {**it, "count": n}
    return sorted(merged.values(),
                  key=lambda x: (x["name"], x["b"].is_copy, -x["b"].material_efficiency))


@router.get("/blueprints")
def blueprints(request: Request, q: str = "", owner: str = "all", kind: str = "all",
               combine: int = 1, db: Session = Depends(get_db)):
    stmt = select(Blueprint)
    if owner in ("corporation", "character"):
        stmt = stmt.where(Blueprint.owner_type == owner)
    if kind == "bpo":
        stmt = stmt.where(Blueprint.quantity != -2)
    elif kind == "bpc":
        stmt = stmt.where(Blueprint.quantity == -2)
    rows = db.scalars(stmt).all()
    names = _names(db, {b.type_id for b in rows} | {b.owner_id for b in rows
                                                     if b.owner_type == "character"})
    if q:
        needle = q.lower()
        rows = [b for b in rows if needle in names.get(b.type_id, "").lower()]

    corps = {c.corporation_id: c for c in db.scalars(select(Corporation))}
    divs = {cid: industry.division_names(db, cid) for cid in corps}
    groups: dict[str, list[dict]] = {}
    for b in rows:
        if b.owner_type == "corporation":
            label = divs.get(b.owner_id, {}).get(b.location_flag)
            group = f"Corp · {label}" if label else f"Corp · {b.location_flag or 'other'}"
        else:
            group = f"{names.get(b.owner_id, b.owner_id)} · personal"
        groups.setdefault(group, []).append({
            "b": b, "name": names.get(b.type_id, f"Type {b.type_id}")})

    view = {}
    for group, items in sorted(groups.items()):
        lines = aggregate_blueprints(items) if combine else sorted(
            ({**it, "count": it["b"].quantity if it["b"].quantity > 0 else 1} for it in items),
            key=lambda x: (x["name"], x["b"].is_copy, -x["b"].material_efficiency))
        view[group] = {"lines": lines, "total": sum(x["count"] for x in lines)}
    total = sum(g["total"] for g in view.values())
    lines = sum(len(g["lines"]) for g in view.values())
    return render(request, "industry_blueprints.html", groups=view, q=q, owner=owner,
                  kind=kind, combine=combine, total=total, lines=lines)


@router.post("/sync-now")
def sync_now(request: Request, csrf: str = Form(""), back: str = Form("/industry/slots")):
    require_csrf(request, csrf)
    from app.jobs.scheduler import trigger_industry_now
    trigger_industry_now()
    flash(request, "Industry sync started. ESI caches skills, jobs and blueprints for a while, "
                   "so changes may take a few minutes to appear.")
    return RedirectResponse(back if back.startswith("/industry") else "/industry/slots",
                            status_code=303)
