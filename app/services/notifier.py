"""Notification outbox. Producers call enqueue(); the dispatcher job delivers via Discord.

The outbox means alerts survive restarts and Discord outages, and the
UNIQUE(ref, kind) constraint guarantees each alert is sent once.
"""
import logging

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.db import session_scope, utcnow
from app.models import Notification

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def enqueue(db: Session, *, ref: str, kind: str, title: str, body: str = "",
            url: str | None = None) -> bool:
    """Queue a notification. Returns False if this (ref, kind) was already queued."""
    stmt = insert(Notification).values(
        ref=ref, kind=kind, title=title[:256], body=body, url=url,
        status="pending", attempts=0, created_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=["ref", "kind"])
    return db.execute(stmt).rowcount > 0


async def dispatch_pending() -> None:
    from app.discord_bot import bot  # late import: avoids cycle at startup

    if not bot.is_ready_for_dm():
        return
    with session_scope() as db:
        pending = db.scalars(
            select(Notification)
            .where(Notification.status == "pending", Notification.attempts < MAX_ATTEMPTS)
            .order_by(Notification.created_at).limit(20)
        ).all()
        for n in pending:
            n.attempts += 1
            try:
                await bot.send_owner(n.title, n.body, n.url, n.kind)
                n.status, n.sent_at, n.error = "sent", utcnow(), None
            except Exception as e:  # noqa: BLE001 — record any delivery failure
                log.warning("Discord delivery failed for %s/%s: %s", n.ref, n.kind, e)
                n.error = str(e)[:500]
                if n.attempts >= MAX_ATTEMPTS:
                    n.status = "failed"
            db.commit()
