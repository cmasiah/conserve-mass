"""Hauling Contracts › Bulk jobs: item-exchange haul contracts, grouped per requester."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db, utcnow
from app.models import BulkJob, Contract, MapSystem
from app.models.couriers import FINISHED_EX, JOB_STATUSES
from app.security import require_csrf, require_user
from app.services import bulk as svc
from app.services import prices
from app.services.assets import clipboard_text, type_names
from app.web.templating import flash, render

router = APIRouter(dependencies=[Depends(require_user)])

VIEWS = {"open": ("new", "accepted", "hauling"), "done": ("delivered", "closed"), "all": None}


def open_job_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(BulkJob).where(
        BulkJob.status.in_(svc.OPEN_JOB))) or 0


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
                  now=utcnow(), open_jobs=open_job_count(db), FINISHED_EX=FINISHED_EX)


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
    return render(request, "contracts_bulk_job.html", job=job, pickups=picks, items=items,
                  hubs=hub_list, nearest=nearest, per_contract=per_contract,
                  dest=db.get(MapSystem, job.dest_system_id) if job.dest_system_id else None,
                  price_source=prices.source_of(db, type_ids), statuses=JOB_STATUSES,
                  clip=clipboard_text([(i["name"], i["qty"]) for i in items]),
                  clip_by_contract={cid: clipboard_text([(i["name"], i["qty"]) for i in rows])
                                    for cid, rows in per_contract.items()},
                  now=utcnow(), open_jobs=open_job_count(db), FINISHED_EX=FINISHED_EX)


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
