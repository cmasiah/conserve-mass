"""Dashboard, contracts, customers, characters, system pages."""
import asyncio
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, utcnow
from app.discord_bot import bot
from app.esi.sso import COURIER_SCOPES, INDUSTRY_SCOPES
from app.models import (
    Character, Contract, ContractEvent, Corporation, Customer, Notification, SyncState,
)
from app.models.couriers import OPEN_STATUSES, WORK_STATUSES
from app.security import require_csrf, require_user
from app.services.backup import run_backup
from app.services.customers import FINISHED, customer_stats
from app.services import updater, version
from app.services.notifier import enqueue
from app.web.templating import flash, render

router = APIRouter(dependencies=[Depends(require_user)])


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db)):
    now = utcnow()
    outstanding = db.scalars(select(Contract).where(Contract.status == "outstanding")
                             .order_by(Contract.date_issued)).all()
    in_progress = db.scalars(select(Contract).where(Contract.status == "in_progress")
                             .order_by(Contract.date_accepted)).all()
    month_ago = now - timedelta(days=30)
    done_30d = db.execute(
        select(func.count(), func.coalesce(func.sum(Contract.reward_isk), 0),
               func.coalesce(func.sum(Contract.volume_m3), 0))
        .where(Contract.status.in_(FINISHED), Contract.date_completed >= month_ago)
    ).one()
    corp = db.scalar(select(Corporation).where(Corporation.is_tracked.is_(True)))
    sync = db.get(SyncState, f"corp_contracts:{corp.corporation_id}") if corp else None
    pending = db.scalar(select(func.count()).select_from(Notification)
                        .where(Notification.status == "pending"))
    return render(request, "dashboard.html", outstanding=outstanding, in_progress=in_progress,
                  done_30d=done_30d, corp=corp, sync=sync, pending=pending, now=now,
                  bot_ready=bot.is_ready_for_dm(), bot_configured=bot.configured)


@router.get("/contracts")
def contracts(request: Request, status: str = "open", q: str = "",
              db: Session = Depends(get_db)):
    stmt = select(Contract).order_by(Contract.date_issued.desc())
    if status == "open":
        stmt = stmt.where(Contract.status.in_(OPEN_STATUSES))
    elif status == "closed":
        stmt = stmt.where(Contract.status.not_in(OPEN_STATUSES))
    elif status != "all":
        stmt = stmt.where(Contract.status == status)
    if q:
        stmt = stmt.join(Customer).where(Customer.name.ilike(f"%{q}%"))
    rows = db.scalars(stmt.limit(500)).unique().all()
    return render(request, "contracts.html", rows=rows, status=status, q=q, now=utcnow())


@router.get("/contracts/{contract_id}")
def contract_detail(request: Request, contract_id: int, db: Session = Depends(get_db)):
    c = db.get(Contract, contract_id)
    if not c:
        raise HTTPException(404)
    stats = customer_stats(db, c.issuer_id)
    return render(request, "contract_detail.html", c=c, stats=stats[0] if stats else None,
                  now=utcnow())


@router.post("/contracts/{contract_id}")
def contract_update(request: Request, contract_id: int, work_status: str = Form(...),
                    notes: str = Form(""), csrf: str = Form(""),
                    db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    c = db.get(Contract, contract_id)
    if not c or work_status not in WORK_STATUSES:
        raise HTTPException(400)
    if c.work_status != work_status:
        db.add(ContractEvent(contract_id=c.contract_id, field="work_status",
                             old_value=c.work_status, new_value=work_status, source="user"))
        c.work_status = work_status
    c.notes = notes.strip() or None
    db.commit()
    flash(request, "Contract updated.")
    return _redirect(f"/contracts/{contract_id}")


@router.get("/customers")
def customers(request: Request, sort: str = "completed", all: int = 0,
              db: Session = Depends(get_db)):
    rows = customer_stats(db, order=sort, completed_only=not all)
    return render(request, "customers.html", rows=rows, sort=sort, show_all=bool(all))


@router.get("/customers/{character_id}")
def customer_detail(request: Request, character_id: int, db: Session = Depends(get_db)):
    stats = customer_stats(db, character_id)
    if not stats:
        raise HTTPException(404)
    rows = db.scalars(select(Contract).where(Contract.issuer_id == character_id)
                      .order_by(Contract.date_issued.desc())).unique().all()
    return render(request, "customer_detail.html", s=stats[0], rows=rows, now=utcnow())


@router.post("/customers/{character_id}")
def customer_update(request: Request, character_id: int, tag: str = Form(""),
                    notes: str = Form(""), csrf: str = Form(""),
                    db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    cust = db.get(Customer, character_id)
    if not cust:
        raise HTTPException(404)
    cust.tag = tag.strip() or None
    cust.notes = notes.strip() or None
    db.commit()
    flash(request, "Customer updated.")
    return _redirect(f"/customers/{character_id}")


@router.get("/characters")
def characters(request: Request, db: Session = Depends(get_db)):
    chars = db.scalars(select(Character).order_by(Character.is_admin.desc(),
                                                  Character.name)).all()
    corp = db.scalar(select(Corporation).where(Corporation.is_tracked.is_(True)))
    return render(request, "characters.html", chars=chars, corp=corp,
                  courier_scopes=set(COURIER_SCOPES), industry_scopes=set(INDUSTRY_SCOPES))


@router.post("/characters/{character_id}/sync")
def set_sync_character(request: Request, character_id: int, csrf: str = Form(""),
                       db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    corp = db.scalar(select(Corporation).where(Corporation.is_tracked.is_(True)))
    char = db.get(Character, character_id)
    if not corp or not char or char.corporation_id != corp.corporation_id:
        flash(request, "That character isn't in the tracked corporation.", "error")
    else:
        corp.sync_character_id = character_id
        db.commit()
        flash(request, f"{char.name} will now poll corp contracts.")
    return _redirect("/characters")


@router.post("/characters/{character_id}/delete")
def delete_character(request: Request, character_id: int, csrf: str = Form(""),
                     db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    char = db.get(Character, character_id)
    if char and character_id != request.session["user"]["character_id"]:
        db.delete(char)
        db.commit()
        flash(request, f"Removed {char.name} and its tokens.")
    else:
        flash(request, "You can't remove the character you're logged in as.", "error")
    return _redirect("/characters")


@router.get("/system")
async def system(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    notes = db.scalars(select(Notification).order_by(Notification.created_at.desc())
                       .limit(25)).all()
    syncs = db.scalars(select(SyncState)).all()
    backups = sorted(s.backup_dir.glob("conserve-mass-*.db"), reverse=True)[:5] \
        if s.backup_dir.exists() else []
    return render(request, "system.html", notes=notes, syncs=syncs, backups=backups,
                  settings=s, bot_ready=bot.is_ready_for_dm(), bot_configured=bot.configured,
                  ver=await version.check(), upd=updater.status(), now=utcnow())


@router.post("/system/check-updates")
async def check_updates(request: Request, csrf: str = Form("")):
    require_csrf(request, csrf)
    info = await version.check(force=True)
    if info.error:
        flash(request, info.error, "error")
    elif info.update_available:
        flash(request, f"Version {info.latest} is available.")
    else:
        flash(request, f"You're up to date ({info.current}).")
    return _redirect("/system")


@router.get("/system/update-status")
def update_status():
    st = updater.status()
    from app.config import APP_VERSION
    return JSONResponse({"version": APP_VERSION, "pending": st.pending,
                         "result": st.last_result, "finished": st.last_finished})


@router.post("/system/update-now")
def update_now(request: Request, csrf: str = Form("")):
    require_csrf(request, csrf)
    st = updater.status()
    if not st.installed:
        flash(request, "The host updater isn't running yet, so the app can't update itself. "
                       "See 'Set up one-click updates' below.", "error")
        return _redirect("/system")
    if not st.pending:
        updater.request_update(request.session["user"]["name"])
    return _redirect("/system?updating=1")


@router.post("/system/test-discord")
def test_discord(request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    enqueue(db, ref=f"test:{utcnow().timestamp()}", kind="test", title="👋 Test from Conserve Mass",
            body="If you can read this, Discord DMs are working.")
    db.commit()
    flash(request, "Test DM queued — it should arrive within ~20s.")
    return _redirect("/system")


@router.post("/system/sync-now")
def sync_now(request: Request, csrf: str = Form("")):
    require_csrf(request, csrf)
    from app.jobs.scheduler import trigger_sync_now
    trigger_sync_now()
    flash(request, "Sync triggered. ESI caches contracts ~5 minutes, so new ones may lag.")
    return _redirect("/")


@router.post("/system/backup-now")
async def backup_now(request: Request, csrf: str = Form("")):
    require_csrf(request, csrf)
    path = await asyncio.to_thread(run_backup)
    flash(request, f"Backup written: {path}")
    return _redirect("/system")


@router.post("/system/retry-failed")
def retry_failed(request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    for n in db.scalars(select(Notification).where(Notification.status == "failed")):
        n.status, n.attempts = "pending", 0
    db.commit()
    flash(request, "Failed notifications re-queued.")
    return _redirect("/system")
