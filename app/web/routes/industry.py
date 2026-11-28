"""Industry pages: slots & skills, job tracker, blueprint library."""
from collections import Counter

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import (
    AssetNode, Blueprint, Character, Corporation, EntityName, EsiToken, IndustryJob, Location,
    MarketGroup, TypeInfo,
)
from app.security import require_csrf, require_user
from app.services import characters, industry, places, sync_info
from app.services.type_info import market_path
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
    """Characters missing personal scopes for what they're switched on for. Corp scopes
    aren't listed: only a director needs those, and members can't grant them anyway."""
    out = []
    for c in db.scalars(select(Character).order_by(Character.name)):
        missing = characters.missing_scopes(c)
        if missing:
            out.append((c, missing))
    return out


@router.get("/slots")
def slots(request: Request, sort: str = "name", dir: str = "", only: str = "",
          db: Session = Depends(get_db)):
    everyone = industry.character_overview(db)
    synced = [r for r in everyone if r.has_skills]
    # Command-center totals always cover every tracked character, whatever the filter.
    totals = {c: {"cap": sum(r.capacity.get(c, 0) for r in synced),
                  "used": sum(r.used.get(c, 0) for r in synced),
                  "free": sum(r.free(c) for r in synced)} for c in CATS}
    capital_builders = sum(1 for r in synced if r.capital_level >= 1)
    gold = industry.GOLD_SLOTS

    rows = list(everyone)
    kind, _, cat = only.partition(":")        # "gold:manufacturing", "under:science"
    if kind == "gold" and cat in CATS:
        rows = [r for r in rows if r.capacity.get(cat, 0) >= gold]
    elif kind == "under" and cat in CATS:
        # Below the gold standard: the characters whose skills are worth training.
        rows = [r for r in rows if r.has_skills and r.capacity.get(cat, 0) < gold]
    elif only == "capital":
        rows = [r for r in rows if r.capital_level >= 1]
    elif kind == "remote" and cat in CATS:
        rows = [r for r in rows if r.has_skills and r.levels.get(industry.REMOTE_SKILLS[cat], 0)]

    # Default direction: names A→Z, everything else biggest first. dir flips it.
    desc = (sort != "name") if dir not in ("asc", "desc") else dir == "desc"
    if sort in CATS:
        rows.sort(key=lambda r: (r.capacity.get(sort, 0), r.free(sort)), reverse=desc)
    elif sort == "capital":
        rows.sort(key=lambda r: (r.capital_level, r.queued.get("Capital Ship Construction", 0)),
                  reverse=desc)
    else:
        sort = "name"
        rows.sort(key=lambda r: (r.character.name or "").lower(), reverse=desc)
    under_counts = {c: sum(1 for r in synced if r.capacity.get(c, 0) < gold) for c in CATS}
    gold_counts = {c: sum(1 for r in synced if r.capacity.get(c, 0) >= gold) for c in CATS}
    remote_counts = {c: sum(1 for r in synced if r.levels.get(industry.REMOTE_SKILLS[c], 0))
                     for c in CATS}
    detail = {r.character.character_id: {c: industry.slot_breakdown(r.levels, c, r.queued)
                                         for c in CATS}
              for r in rows if r.has_skills}
    return render(request, "industry_slots.html", rows=rows, sort=sort, only=only, cats=CATS,
                  desc=desc, gold=gold, under_counts=under_counts, gold_counts=gold_counts,
                  remote_counts=remote_counts,
                  labels=industry.CATEGORY_LABELS, totals=totals, detail=detail,
                  tracked=len(everyone), synced=len(synced), capital_builders=capital_builders,
                  tiers=industry.CAPITAL_TIERS, tier_rows=industry.CAPITAL_TIER_ROWS,
                  missing=_missing_scopes(db), now=utcnow(),
                  sync_feeds=sync_info.feeds(db, "industry"))


# Activities whose "product" is itself a blueprint, so the icon must be a blueprint image.
BLUEPRINT_PRODUCT = {3: "bp", 4: "bp", 5: "bpc", 8: "bpc"}  # TE, ME, copying, invention


def job_icon(activity_id: int, product_type_id: int | None, blueprint_type_id: int) -> str:
    """images.evetech.net path for a job's item. Research/copy/invention produce blueprints,
    which need the bp/bpc variant (the plain `icon` variant shows the wrong picture)."""
    variant = BLUEPRINT_PRODUCT.get(activity_id)
    if variant:
        return f"types/{product_type_id or blueprint_type_id}/{variant}"
    return f"types/{product_type_id or blueprint_type_id}/icon"


def group_jobs(view: list[dict]) -> list[dict]:
    """Collapse identical jobs (same activity and item) into one expandable line."""
    groups: dict[tuple, dict] = {}
    for v in view:
        j = v["j"]
        key = (j.activity_id, j.product_type_id or j.blueprint_type_id)
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"key": f"{key[0]}-{key[1]}", "what": v["what"],
                               "icon": v["icon"], "activity": v["activity"], "jobs": []}
        g["jobs"].append(v)
    out = []
    for g in groups.values():
        jobs = sorted(g["jobs"], key=lambda v: v["j"].end_date)
        g["jobs"] = jobs
        g["count"] = len(jobs)
        g["runs"] = sum(v["j"].runs for v in jobs)
        g["ready"] = sum(1 for v in jobs if v["done"])
        g["first_end"] = jobs[0]["j"].end_date
        g["last_end"] = jobs[-1]["j"].end_date
        g["pct"] = sum(v["pct"] for v in jobs) / len(jobs)
        g["who"] = sorted({v["who"] for v in jobs})
        g["owners"] = sorted({v["owner"] for v in jobs})
        out.append(g)
    out.sort(key=lambda g: (g["ready"] == 0, g["first_end"]))
    return out


@router.get("/jobs")
def jobs(request: Request, cat: str = "", who: int = 0, owner: str = "",
         db: Session = Depends(get_db)):
    q = select(IndustryJob).order_by(IndustryJob.end_date)
    if who:
        q = q.where(IndustryJob.installer_id == who)
    if owner in ("corporation", "character"):
        q = q.where(IndustryJob.owner_type == owner)
    else:
        owner = ""
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
                     "icon": job_icon(j.activity_id, j.product_type_id, j.blueprint_type_id),
                     "who": names.get(j.installer_id, str(j.installer_id)),
                     "activity": industry.ACTIVITY_NAMES.get(j.activity_id,
                                                             f"Activity {j.activity_id}"),
                     "owner": (corps[j.owner_id].ticker or "Corp") if j.owner_type ==
                     "corporation" and j.owner_id in corps else "Personal"})
    ready = sum(1 for v in view if v["done"])
    return render(request, "industry_jobs.html", groups=group_jobs(view), total=len(view),
                  cat=cat, who=who, owner=owner, cats=CATS, labels=industry.CATEGORY_LABELS,
                  installers=installers, ready=ready, now=now,
                  sync_feeds=sync_info.feeds(db, "industry", now))


def blueprint_kind(name: str) -> str:
    """Reaction formulas are all named "... Reaction Formula" in EVE."""
    return "reactions" if name.endswith("Reaction Formula") else "manufacturing"


def aggregate_blueprints(items: list[dict]) -> list[dict]:
    """Merge identical blueprints into one line with a count.

    Identical = same type, original/copy, ME, TE and runs. A stacked BPO
    (quantity > 0) counts as that many blueprints. `where` tallies where they're held.
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
            merged[key] = {**it, "count": n, "where": Counter()}
        merged[key]["where"][it.get("where", "")] += n
    return sorted(merged.values(),
                  key=lambda x: (-x["count"], x["name"], x["b"].is_copy))


# Friendlier names for EVE's top-level blueprint market groups.
SECTION_LABELS = {"Ship Equipment": "Modules", "Ship Modifications": "Rigs",
                  "Ammunition & Charges": "Ammo & Charges",
                  "Manufacture & Research": "Components & Materials"}
UNCATEGORIZED = "Not categorized yet"


def blueprint_category(path: list[str]) -> tuple[str, str]:
    """(section, subsection) from a market path like
    ['Blueprints & Reactions', 'Ammunition & Charges', 'Mining Crystals', ...]."""
    if path and path[0].startswith("Blueprints"):
        path = path[1:]
    if not path:
        return UNCATEGORIZED, ""
    section = SECTION_LABELS.get(path[0], path[0])
    return section, (path[1] if len(path) > 1 else "")


@router.get("/blueprints")
def blueprints(request: Request, q: str = "", owner: str = "all", kind: str = "all",
               combine: int = 1, cat: str = "", sub: str = "", db: Session = Depends(get_db)):
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

    # Categories (market groups) and places (structures), all from the local cache.
    infos = {t.type_id: t for t in db.scalars(select(TypeInfo).where(
        TypeInfo.type_id.in_({b.type_id for b in rows})))}
    groups_tree = {g.market_group_id: g for g in db.scalars(select(MarketGroup))}
    nodes: dict[tuple, dict[int, AssetNode]] = {}
    for n in db.scalars(select(AssetNode)):
        nodes.setdefault((n.owner_type, n.owner_id), {})[n.item_id] = n
    locations = {loc.location_id: loc for loc in db.scalars(select(Location))}
    corps = {c.corporation_id: c for c in db.scalars(select(Corporation))}
    divs = {cid: industry.division_names(db, cid) for cid in corps}
    pilots = {c.character_id: c.name for c in db.scalars(select(Character))}

    items, cat_counts, sub_counts = [], Counter(), Counter()
    for b in rows:
        name = names.get(b.type_id, f"Type {b.type_id}")
        ti = infos.get(b.type_id)
        section, subsection = blueprint_category(market_path(groups_tree, ti.market_group_id)
                                                 if ti and ti.market_group_id else [])
        n = b.quantity if b.quantity > 0 else 1
        cat_counts[section] += n
        if cat and section != cat:
            continue
        if subsection:
            sub_counts[subsection] += n
        if sub and subsection != sub:
            continue
        place = places.describe(
            b.location_id, b.location_flag, nodes.get((b.owner_type, b.owner_id), {}),
            locations, divs.get(b.owner_id) if b.owner_type == "corporation" else None)
        who = "" if b.owner_type == "corporation" else \
            f"{pilots.get(b.owner_id) or names.get(b.owner_id, b.owner_id)} (personal)"
        detail = " · ".join(x for x in (place.detail, who) if x)
        where = f"{place.site}\x1f{detail}"
        items.append({"b": b, "name": name, "where": where, "section": section,
                      "subsection": subsection or section})

    sections: dict[str, list[dict]] = {"manufacturing": [], "reactions": []}
    for it in items:
        sections[blueprint_kind(it["name"])].append(it)
    view = {}
    for key, its in sections.items():
        if not its:
            continue
        if combine:
            lines = aggregate_blueprints(its)
        else:
            lines = [{**it, "count": it["b"].quantity if it["b"].quantity > 0 else 1,
                      "where": Counter({it["where"]: 1})} for it in its]
            lines.sort(key=lambda x: (x["name"], x["b"].is_copy))
        view[key] = {"label": "Reaction formulas" if key == "reactions" else "Manufacturing",
                     "lines": lines, "total": sum(x["count"] for x in lines)}
    total = sum(g["total"] for g in view.values())
    lines = sum(len(g["lines"]) for g in view.values())
    order = sorted(cat_counts, key=lambda c: (c == UNCATEGORIZED, -cat_counts[c], c))
    return render(request, "industry_blueprints.html", groups=view, q=q, owner=owner,
                  kind=kind, combine=combine, total=total, lines=lines, now=utcnow(),
                  cat=cat, sub=sub, categories=[(c, cat_counts[c]) for c in order],
                  subcategories=sorted(sub_counts.items(), key=lambda x: (-x[1], x[0])),
                  all_count=sum(cat_counts.values()),
                  sync_feeds=sync_info.feeds(db, "industry"))


@router.post("/sync-now")
def sync_now(request: Request, csrf: str = Form(""), back: str = Form("/industry/slots"),
             db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    from app.jobs.scheduler import trigger_industry_now
    trigger_industry_now()
    flash(request, sync_info.pressed_message(db, "industry"))
    ok = back.startswith("/industry") or back.startswith("/characters")
    return RedirectResponse(back if ok else "/industry/slots", status_code=303)
