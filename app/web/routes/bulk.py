"""Hauling Contracts › Bulk jobs: item-exchange haul contracts, grouped per requester."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
import json

from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import BulkJob, Contract, MapSystem
from app.models.couriers import FINISHED_EX, JOB_STATUSES
from app.security import require_csrf, require_user
from app.services import bulk as svc
from app.services import bulk_plan
from app.services import prices
from app.services.assets import clipboard_text, type_names
from app.web.templating import flash, render

router = APIRouter(dependencies=[Depends(require_user)])

VIEWS = {"open": ("new", "accepted", "hauling"), "done": ("delivered", "closed"), "all": None}


def open_job_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(BulkJob).where(
        BulkJob.status.in_(svc.OPEN_JOB))) or 0


def new_job_count(db: Session) -> int:
    """Jobs with nothing accepted yet: they need you to accept contracts in game."""
    return db.scalar(select(func.count()).select_from(BulkJob).where(
        BulkJob.status == "new")) or 0


def waiting_job_count(db: Session) -> int:
    """Open jobs that still have contracts for you to accept (new or partly accepted)."""
    return sum(1 for j in db.scalars(select(BulkJob).where(BulkJob.status.in_(svc.OPEN_JOB)))
               if j.waiting)


def _items(db: Session, contracts: list[Contract]) -> list[dict]:
    """Everything in these contracts, one line per item type (copies apart)."""
    lines: dict[tuple[int, bool], dict] = {}
    for c in contracts:
        for it in c.items:
            if not it.is_included:
                continue
            ln = lines.setdefault((it.type_id, it.is_bpc), {
                "type_id": it.type_id, "bpc": it.is_bpc, "qty": 0, "value": 0.0})
            ln["qty"] += it.quantity
            ln["value"] += (it.unit_value or 0) * it.quantity
    names = type_names(db, {k[0] for k in lines})
    for (tid, bpc), ln in lines.items():
        ln["name"] = names.get(tid, f"Type {tid}") + (" (Copy)" if bpc else "")
    return sorted(lines.values(), key=lambda x: -x["value"])


@router.get("/contracts/bulk")
def bulk_list(request: Request, view: str = "open", db: Session = Depends(get_db)):
    view = view if view in VIEWS else "open"
    stmt = select(BulkJob).order_by(BulkJob.last_issued_at.desc())
    if VIEWS[view]:
        stmt = stmt.where(BulkJob.status.in_(VIEWS[view]))
    jobs = db.scalars(stmt.limit(300)).all()
    dests = {s.system_id: s for s in db.scalars(select(MapSystem).where(
        MapSystem.system_id.in_({j.dest_system_id for j in jobs if j.dest_system_id})))}
    return render(request, "contracts_bulk.html", jobs=jobs, view=view, dests=dests,
                  now=utcnow(), open_jobs=open_job_count(db), new_jobs=new_job_count(db),
                  waiting_jobs=waiting_job_count(db),
                  FINISHED_EX=FINISHED_EX)


@router.get("/contracts/bulk/{job_id}")
def bulk_job(request: Request, job_id: int, db: Session = Depends(get_db)):
    job = db.get(BulkJob, job_id)
    if job is None:
        raise HTTPException(404)
    picks = svc.pickups(db, job)
    items = _items(db, job.contracts)
    hub_list = svc.hub_systems(db)
    nearest = svc.nearest_hub(db, job)
    type_ids = {it.type_id for c in job.contracts for it in c.items}
    per_contract = {c.contract_id: _items(db, [c]) for c in job.contracts}
    plan = json.loads(job.plan.data) if job.plan else None
    done = set(json.loads(job.plan.done)) if job.plan else set()
    trips = []
    for t in (plan or {}).get("trips", []):
        st = bulk_plan.steps(t)
        trips.append({"t": t, "steps": st, "done": sum(1 for s in st if s["id"] in done),
                      "clip": {s["id"]: clipboard_text([(i["name"], i["qty"]) for i in s["stop"]["items"]])
                               for s in st if s["kind"] == "load"}})
    stale = bool(job.plan and job.plan.fingerprint != bulk_plan.current_fingerprint(db, job))
    total_steps = sum(len(x["steps"]) for x in trips)
    return render(request, "contracts_bulk_job.html", job=job, pickups=picks, items=items,
                  hubs=hub_list, nearest=nearest, per_contract=per_contract,
                  dest=db.get(MapSystem, job.dest_system_id) if job.dest_system_id else None,
                  price_source=prices.source_of(db, type_ids), statuses=JOB_STATUSES,
                  plan=plan, trips=trips, done=done, stale=stale, total_steps=total_steps,
                  progress=round(100 * len(done) / total_steps) if total_steps else 0,
                  cost=bulk_plan.costs(db, job, plan) if plan else None,
                  pricing=bulk_plan.pricing_settings(db), still_there=_still_there(db, job, picks),
                  clip=clipboard_text([(i["name"], i["qty"]) for i in items]),
                  clip_by_contract={cid: clipboard_text([(i["name"], i["qty"]) for i in rows])
                                    for cid, rows in per_contract.items()},
                  now=utcnow(), open_jobs=open_job_count(db), new_jobs=new_job_count(db),
                  waiting_jobs=waiting_job_count(db),
                  FINISHED_EX=FINISHED_EX)


def _still_there(db: Session, job: BulkJob, picks) -> dict[int, dict]:
    """Per pickup system: the job's goods still sitting at its pickup structures, from the
    corp's synced assets (count of items and m3)."""
    from app.models import Asset
    from app.services.jump import _get_json
    vols = {int(k): v for k, v in _get_json(db, bulk_plan.VOLUMES_KEY, {}).items()}
    types = {it.type_id for c in job.live for it in c.items}
    out = {}
    for p in picks:
        if not p.system or not p.location_ids or not types:
            continue
        rows = db.execute(select(Asset.type_id, func.sum(Asset.quantity)).where(
            Asset.site_id.in_(p.location_ids), Asset.type_id.in_(types)).group_by(Asset.type_id)).all()
        qty = sum(int(q or 0) for _t, q in rows)
        m3 = sum((vols.get(t, 0) or 0) * int(q or 0) for t, q in rows)
        accepted = any(c.status in FINISHED_EX for c in p.contracts)
        out[p.system.system_id] = {"qty": qty, "m3": m3, "cleared": accepted and qty == 0}
    return out


def _isk(text: str) -> float | None:
    """'1.8b', '250m', '12,500,000' -> ISK; blank -> None."""
    t = (text or "").strip().lower().replace(",", "").replace(" ", "").replace("isk", "")
    if not t:
        return None
    mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get(t[-1], 1)
    try:
        return float(t[:-1] if mult != 1 else t) * mult
    except ValueError:
        return None


@router.post("/contracts/bulk/{job_id}")
def bulk_update(request: Request, job_id: int, csrf: str = Form(""), status: str = Form(""),
                dest: str = Form(""), agreed: str = Form(""), collateral: str = Form(""),
                title: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    job = db.get(BulkJob, job_id)
    if job is None:
        raise HTTPException(404)
    if status in JOB_STATUSES and status != job.status:
        job.status = status
        if status == "delivered" and not job.delivered_at:
            job.delivered_at = utcnow()
    if dest == "auto":
        job.dest_override = False
        svc.choose_destination(db, job)
    elif dest.isdigit():
        job.dest_system_id, job.dest_override = int(dest), True
    job.agreed_reward_isk = _isk(agreed)
    job.return_collateral_isk = _isk(collateral)
    job.title = title.strip()[:128] or None
    job.notes = notes.strip() or None
    db.commit()
    flash(request, "Job saved.")
    return RedirectResponse(f"/contracts/bulk/{job_id}", status_code=303)


def _fetch(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


@router.post("/contracts/bulk/{job_id}/plan")
async def bulk_plan_now(request: Request, job_id: int, csrf: str = Form("")):
    """Plan (or re-plan) the job's trips. Started trips are kept."""
    require_csrf(request, csrf)
    from app.services.jump import ISOTOPES
    await prices.ensure(set(ISOTOPES))
    result = await bulk_plan.make_plan(job_id)
    if result.get("error"):
        flash(request, result["error"], "error")
    else:
        t = result["totals"]
        msg = f"Planned {t['trips']} trip{'s' if t['trips'] != 1 else ''} in {t['waves']} wave{'s' if t['waves'] != 1 else ''}, {t['jumps']} jumps."
        if result.get("unreachable"):
            msg += f" Can't reach: {', '.join(result['unreachable'])}."
        if result.get("too_big"):
            msg += f" Too big for any hold: {', '.join(result['too_big'])}."
        flash(request, msg)
    return RedirectResponse(f"/contracts/bulk/{job_id}#plan", status_code=303)


@router.post("/contracts/bulk/{job_id}/step")
def bulk_step(request: Request, job_id: int, csrf: str = Form(""), step: str = Form(""),
              done: int = Form(1), db: Session = Depends(get_db)):
    """Tick (or untick) one step of the saved plan."""
    require_csrf(request, csrf)
    job = db.get(BulkJob, job_id)
    if job is None or job.plan is None:
        raise HTTPException(404)
    ids = set(json.loads(job.plan.done))
    (ids.add if done else ids.discard)(step)
    job.plan.done = json.dumps(sorted(ids))
    job.plan.updated_at = utcnow()
    if ids and job.status in ("new", "accepted"):
        job.status = "hauling"
    db.commit()
    if _fetch(request):
        return JSONResponse({"ok": True, "done": len(ids)})
    return RedirectResponse(f"/contracts/bulk/{job_id}#plan", status_code=303)


@router.post("/contracts/bulk/{job_id}/pricing")
def bulk_pricing(request: Request, job_id: int, csrf: str = Form(""), isk_per_jump: str = Form(""),
                 risk_pct: str = Form(""), use: str = Form(""), db: Session = Depends(get_db)):
    """Save the pricing defaults (ISK per jump, risk %), or take a suggested reward."""
    require_csrf(request, csrf)
    job = db.get(BulkJob, job_id)
    if job is None:
        raise HTTPException(404)
    if use:
        job.agreed_reward_isk = _isk(use)
        flash(request, "Reward set. Contract it back for that, and the app will spot it.")
    else:
        try:
            risk = float((risk_pct or "0").replace("%", "") or 0)
        except ValueError:
            risk = 0.0
        bulk_plan.save_pricing(db, _isk(isk_per_jump) or 0.0, risk)
        flash(request, "Pricing defaults saved (they apply to every job).")
    db.commit()
    return RedirectResponse(f"/contracts/bulk/{job_id}#pricing", status_code=303)
