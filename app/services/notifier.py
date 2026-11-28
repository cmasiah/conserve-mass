"""Notification outbox. Producers call enqueue(); the dispatcher job delivers via Discord.

The outbox means alerts survive restarts and Discord outages, and the
UNIQUE(ref, kind) constraint guarantees each alert is sent once.

Flood protection (e.g. someone posts 100 contracts at once):
  * more than `digest_threshold` pending alerts of one kind -> ONE digest message;
  * at most MAX_MESSAGES_PER_RUN messages per dispatch run (every 20 s);
  * at most `discord_max_messages_per_hour` messages per hour; the rest wait and are
    digested on a later run;
  * alerts about muted/blocked issuers are dropped (see cancel_for_subject).
"""
import logging
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import release_write_lock, session_scope, utcnow
from app.models import Notification

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 5
MAX_MESSAGES_PER_RUN = 3
DIGEST_LINES = 15

# Kinds that may be merged into a digest. One-off alerts (spam warning, token revoked,
# update available, test) are always sent on their own.
DIGEST_TITLES = {
    "new_contract": "🚚 {n} new courier contracts",
    "expiring": "⏳ {n} unaccepted contracts expiring soon",
    "delivery_due": "⏰ {n} deliveries due soon",
    "status_accepted": "📦 {n} contracts accepted",
    "status_finished": "✅ {n} contracts delivered",
    "status_failed": "❌ {n} contracts FAILED",
    "status_cancelled": "🚫 {n} contracts cancelled",
    "status_deleted": "🗑️ {n} contracts deleted",
    "status_rejected": "↩️ {n} contracts rejected",
    "industry_job_ready": "🏭 {n} watched industry jobs ready for delivery",
    "research_job_ready": "🔬 {n} research jobs finished",
}

_sent_times: deque[float] = deque()  # monotonic timestamps of messages sent (last hour)


def enqueue(db: Session, *, ref: str, kind: str, title: str, body: str = "",
            url: str | None = None, line: str | None = None,
            subject_id: int | None = None) -> bool:
    """Queue a notification. Returns False if it was already queued, or if you've
    switched this kind of notification off on the Notifications settings page."""
    from app.services import prefs
    if not prefs.is_enabled(db, kind):
        return False
    stmt = insert(Notification).values(
        ref=ref, kind=kind, title=title[:256], body=body, url=url, line=line,
        subject_id=subject_id, status="pending", attempts=0, created_at=utcnow(),
    ).on_conflict_do_nothing(index_elements=["ref", "kind"])
    return db.execute(stmt).rowcount > 0


def cancel_for_subject(db: Session, subject_id: int) -> int:
    """Drop pending alerts about an issuer (used when they're muted or blocked)."""
    res = db.execute(update(Notification)
                     .where(Notification.subject_id == subject_id,
                            Notification.status == "pending")
                     .values(status="dropped", error="issuer muted/blocked"))
    return res.rowcount


@dataclass
class Message:
    kind: str
    items: list
    digest: bool


def plan_messages(pending: list, threshold: int, limit: int) -> list[Message]:
    """Group pending notifications into at most `limit` messages, oldest first."""
    groups: OrderedDict[str, list] = OrderedDict()
    for n in pending:
        groups.setdefault(n.kind, []).append(n)
    msgs: list[Message] = []
    for kind, items in groups.items():
        if kind in DIGEST_TITLES and len(items) > threshold:
            msgs.append(Message(kind, items, True))
        else:
            msgs.extend(Message(kind, [n], False) for n in items)
    return msgs[:max(limit, 0)]


def digest_text(kind: str, items: list) -> tuple[str, str]:
    title = DIGEST_TITLES[kind].format(n=len(items))
    lines = [f"• {n.line or n.title}" for n in items[:DIGEST_LINES]]
    if len(items) > DIGEST_LINES:
        lines.append(f"…and {len(items) - DIGEST_LINES} more on the dashboard.")
    return title, "\n".join(lines)


def group_for_display(notes: list, window_seconds: float = 0.05) -> list[dict]:
    """Fold alerts that went out as one Discord digest into one line for the System page.

    Notes must be newest first. Sent alerts of a digestible kind sent within
    `window_seconds` of each other were delivered together (separate messages are further
    apart, since each one waits for Discord).
    """
    out: list[dict] = []
    for n in notes:
        last = out[-1] if out else None
        if (last and n.status == "sent" and n.kind in DIGEST_TITLES
                and last["status"] == "sent" and last["kind"] == n.kind
                and n.sent_at and last["sent_at"]
                and abs((last["sent_at"] - n.sent_at).total_seconds()) <= window_seconds):
            last["items"].append(n)
            continue
        out.append({"kind": n.kind, "status": n.status, "sent_at": n.sent_at,
                    "created_at": n.created_at, "error": n.error, "items": [n]})
    for g in out:
        if len(g["items"]) > 1:
            g["title"] = DIGEST_TITLES[g["kind"]].format(n=len(g["items"]))
            g["created_at"] = min(i.created_at for i in g["items"])
        else:
            g["title"] = g["items"][0].title
    return out


def _hourly_budget() -> int:
    now = time.monotonic()
    while _sent_times and now - _sent_times[0] > 3600:
        _sent_times.popleft()
    return get_settings().discord_max_messages_per_hour - len(_sent_times)


async def dispatch_pending() -> None:
    from app.discord_bot import bot  # late import: avoids cycle at startup

    if not bot.is_ready_for_dm():
        return
    budget = min(MAX_MESSAGES_PER_RUN, _hourly_budget())
    if budget <= 0:
        return  # over the hourly cap; pending alerts wait and get digested later

    s = get_settings()
    with session_scope() as db:
        pending = db.scalars(
            select(Notification)
            .where(Notification.status == "pending", Notification.attempts < MAX_ATTEMPTS)
            .order_by(Notification.created_at).limit(1000)
        ).all()
        for msg in plan_messages(pending, s.digest_threshold, budget):
            if msg.digest:
                title, body = digest_text(msg.kind, msg.items)
                url = f"{s.base_url}/"
            else:
                n = msg.items[0]
                title, body, url = n.title, n.body, n.url
            for n in msg.items:
                n.attempts += 1
            try:
                release_write_lock("Discord send")
                await bot.send_owner(title, body, url, msg.kind)
                _sent_times.append(time.monotonic())
                sent_at = utcnow()   # one timestamp per message, so a digest stays groupable
                for n in msg.items:
                    n.status, n.sent_at, n.error = "sent", sent_at, None
            except Exception as e:  # noqa: BLE001 — record any delivery failure
                log.warning("Discord delivery failed (%s, %d alert(s)): %s",
                            msg.kind, len(msg.items), e)
                for n in msg.items:
                    n.error = str(e)[:500]
                    if n.attempts >= MAX_ATTEMPTS:
                        n.status = "failed"
            db.commit()
