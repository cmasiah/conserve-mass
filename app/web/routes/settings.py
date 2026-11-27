"""Notification preferences page."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.security import require_csrf, require_user
from app.services import prefs
from app.web.templating import flash, render

router = APIRouter(prefix="/settings", dependencies=[Depends(require_user)])


@router.get("/notifications")
def notifications(request: Request, db: Session = Depends(get_db)):
    return render(request, "settings_notifications.html", **prefs.snapshot(db))


@router.post("/notifications")
async def save_notifications(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    require_csrf(request, form.get("csrf"))
    prefs.save(db, {k: str(v) for k, v in form.items()})
    flash(request, "Notification preferences saved.")
    return RedirectResponse("/settings/notifications", status_code=303)
