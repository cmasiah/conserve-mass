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
from app.esi.sso import (
    COURIER_SCOPES, CORP_INDUSTRY_SCOPES, PERSONAL_SCOPES, PROFILE_LABELS, SCOPE_PROFILES,
)
from app.models import (
    Character, Contract, ContractEvent, Corporation, Customer, EntityName, Notification,
    SyncState,
)
from app.models.couriers import OPEN_STATUSES, WORK_STATUSES
from app.security import require_csrf, require_user
from app.services.backup import run_backup
from app.services.customers import FINISHED, customer_stats
from app.services import characters as char_svc, home as home_svc, sync_info, updater, version
from app.services.notifier import cancel_for_subject, enqueue, group_for_display
from app.web.templating import flash, render

router = APIRouter(dependencies=[Depends(require_user)])


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)):
    info = version.cached()
    data = home_svc.build(db, bot_configured=bot.configured, bot_ready=bot.is_ready_for_dm(),
                          update_available=info.update_available, latest=info.latest_label)
    return render(request, "home.html", **data)


@router.get("/hauling")
def dashboard(request: Request, db: Session = Depends(get_db)):
    now = utcnow()
    not_blocked = Contract.issuer_id.not_in(
        select(Customer.character_id).where(Customer.blocked.is_(True)))
    outstanding = db.scalars(select(Contract).where(Contract.status == "outstanding", not_blocked)
                             .order_by(Contract.date_issued)).all()
    in_progress = db.scalars(select(Contract).where(Contract.status == "in_progress", not_blocked)
                             .order_by(Contract.date_accepted)).all()
    hidden_blocked = db.scalar(select(func.count()).select_from(Contract).where(
        Contract.status.in_(OPEN_STATUSES), ~not_blocked))
    flagged = db.scalars(select(Customer).where(Customer.muted.is_(True),
                                                Customer.flagged_at.is_not(None),
                                                Customer.flagged_at >= now - timedelta(days=1))
                         ).all()
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
                  bot_ready=bot.is_ready_for_dm(), bot_configured=bot.configured,
                  hidden_blocked=hidden_blocked, flagged=flagged,
                  sync_feeds=sync_info.feeds(db, "contracts", now))


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


@router.post("/customers/{character_id}/moderate")
def customer_moderate(request: Request, character_id: int, action: str = Form(...),
                      reason: str = Form(""), csrf: str = Form(""),
                      db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    cust = db.get(Customer, character_id)
    if not cust or action not in ("mute", "unmute", "block", "unblock"):
        raise HTTPException(400)
    who = cust.name or character_id
    if action in ("mute", "block"):
        if action == "mute":
            cust.muted = True
        else:
            cust.blocked = True
        cust.flag_reason = reason.strip() or cust.flag_reason or f"{action}d manually"
        cust.flagged_at = utcnow()
        dropped = cancel_for_subject(db, character_id)
        msg = (f"{who} {'muted' if action == 'mute' else 'blocked'}. "
               f"{dropped} pending alert(s) dropped.")
        if action == "block":
            msg += " Remember to reject their contracts in-game."
    else:
        if action == "unblock":
            cust.blocked = False
        else:
            cust.muted = False
        if not cust.muted and not cust.blocked:
            cust.flag_reason, cust.flagged_at = None, None
        msg = f"{who} {'unmuted' if action == 'unmute' else 'unblocked'}."
    db.commit()
    flash(request, msg)
    return _redirect(f"/customers/{character_id}")


@router.get("/characters")
def characters(request: Request, db: Session = Depends(get_db)):
    chars = db.scalars(select(Character).order_by(Character.is_admin.desc(),
                                                  Character.name)).all()
    corp = db.scalar(select(Corporation).where(Corporation.is_tracked.is_(True)))
    corp_ids = {c.corporation_id for c in chars if c.corporation_id}
    corp_names = {e.entity_id: e.name for e in db.scalars(
        select(EntityName).where(EntityName.entity_id.in_(corp_ids)))}
    return render(request, "characters.html", chars=chars, corp=corp, corp_names=corp_names,
                  toggles=char_svc.TOGGLES, missing={c.character_id: char_svc.missing_scopes(c)
                                                     for c in chars},
                  courier_scopes=set(COURIER_SCOPES), personal_scopes=set(PERSONAL_SCOPES),
                  corp_scopes=set(CORP_INDUSTRY_SCOPES))


@router.post("/characters/{character_id}/track")
def set_tracking(request: Request, character_id: int, csrf: str = Form(""),
                 what: str = Form(""), on: str = Form(""), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    char = db.get(Character, character_id)
    if char is None or what not in char_svc.BY_KEY:
        raise HTTPException(404)
    enabled = on == "1"
    char_svc.set_tracking(db, char, what, enabled)
    db.commit()
    flash(request, f"{char.name}: {char_svc.BY_KEY[what].label} "
                   f"{'on' if enabled else 'off'}.")
    return _redirect(f"/characters#c{character_id}")


def _onboard_profile(value: str | None) -> str:
    return value if value in ("personal", "industry") else "industry"


@router.get("/characters/onboard/start")
def onboard_start(request: Request, profile: str = "industry"):
    """Start bulk linking: every EVE login comes back to the onboarding page."""
    profile = _onboard_profile(profile)
    request.session["onboard"] = {"linked": [], "profile": profile}
    return _redirect(f"/auth/login?profile={profile}")


@router.get("/characters/onboard")
def onboard(request: Request, db: Session = Depends(get_db)):
    state = request.session.get("onboard")
    if state is None:
        return _redirect("/characters")
    profile = _onboard_profile(state.get("profile"))
    need = set(SCOPE_PROFILES[profile])
    chars = db.scalars(select(Character).order_by(Character.name)).all()
    ready = [c for c in chars if c.token and c.token.is_valid and need <= c.token.scope_set]
    pending = [c for c in chars if c not in ready]
    return render(request, "onboard.html", linked=state.get("linked", []), ready=ready,
                  pending=pending, total=len(chars), profile=profile,
                  profile_label=PROFILE_LABELS[profile])


@router.post("/characters/onboard/stop")
def onboard_stop(request: Request, csrf: str = Form("")):
    require_csrf(request, csrf)
    state = request.session.pop("onboard", None) or {}
    n = len(state.get("linked", []))
    flash(request, f"Linked {n} character{'s' if n != 1 else ''}. Skills and jobs sync within "
                   "minutes, blueprints within the hour (Sync Now on the Slots page explains the "
                   "timing).")
    return _redirect("/characters")


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
        flash(request, f"{char.name} is now the contract poller: corp contracts are read "
                       "with its ESI token.")
    return _redirect("/characters")


@router.post("/characters/{character_id}/delete")
def delete_character(request: Request, character_id: int, csrf: str = Form(""),
                     db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    char = db.get(Character, character_id)
    if char and character_id != request.session["user"]["character_id"]:
        char_svc.remove(db, char)
        db.commit()
        flash(request, f"Removed {char.name}, its ESI token and its synced skills, jobs and "
                       "blueprints. Link it again any time to bring it back.")
    else:
        flash(request, "You can't remove the character you're logged in as.", "error")
    return _redirect("/characters")


@router.get("/system")
async def system(request: Request, db: Session = Depends(get_db)):
    s = get_settings()
    notes = db.scalars(select(Notification).order_by(
        func.coalesce(Notification.sent_at, Notification.created_at).desc()).limit(60)).all()
    notes = group_for_display(notes)[:20]
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
        flash(request, f"Version {info.latest_label} is available.")
    else:
        flash(request, f"You're up to date ({info.current_label}).")
    return _redirect("/system")


@router.get("/system/update-status")
def update_status():
    st = updater.status()
    return JSONResponse({"version": version.cached().current_label, "pending": st.pending,
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
    flash(request, "Test message queued. It should arrive in Discord within ~20 seconds.")
    return _redirect("/system")


@router.post("/system/sync-now")
def sync_now(request: Request, csrf: str = Form(""), db: Session = Depends(get_db)):
    require_csrf(request, csrf)
    from app.jobs.scheduler import trigger_sync_now
    trigger_sync_now()
    flash(request, sync_info.pressed_message(db, "contracts"))
    return _redirect("/hauling")


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
