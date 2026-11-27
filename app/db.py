"""Database engine/session. SQLite in WAL mode, stored in /config.

Locking rules (why "database is locked" used to happen, and how it's prevented)
-----------------------------------------------------------------------------
SQLite allows one writer at a time. A session takes the write lock at its first
INSERT/UPDATE/DELETE and keeps it until commit. Everything async here (web handlers,
scheduled syncs, the Discord bot) shares ONE event loop, and SQLite calls block that
loop while they wait for the lock. So if a sync wrote something and then awaited ESI,
any other coroutine that tried to write froze the loop waiting for a lock the sync
could never release (it needed the loop to finish) -> "database is locked" -> 500.

The rule: never hold a write transaction across a network wait.
  * release_write_lock() commits the current task's pending write transaction. The
    ESI/SSO clients and the Discord sender call it before every request, so the rule
    holds even for code that forgets.
  * Code that must be all-or-nothing does its network calls first and its writes after,
    with no awaits in between (contract sync, the login callback).
  * Leftover contention (a short busy moment) is retried by with_retry(), and the web
    UI shows a friendly "busy, try again" page instead of a bare 500.
"""
import asyncio
import logging
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import TypeVar

from sqlalchemy import DateTime, TypeDecorator, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """Stores naive UTC in SQLite, always returns timezone-aware UTC datetimes."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


log = logging.getLogger(__name__)
T = TypeVar("T")

BUSY_TIMEOUT_S = 8


def _make_engine(url: str):
    # timeout = how long a statement waits for another writer before giving up. Writes are
    # kept short (see module docstring), so a long wait means something is wrong; better
    # to fail, show "busy, try again" and retry than to freeze the app for 30s.
    engine = create_engine(url, connect_args={"check_same_thread": False,
                                              "timeout": BUSY_TIMEOUT_S})

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
            cur.close()

    return engine


engine = _make_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


# ------------------------------------------------------------------ write-lock guard

# Sessions that have touched the database, with the task (or thread) using them.
_live: "weakref.WeakSet[Session]" = weakref.WeakSet()


def _owner() -> object:
    """The asyncio task running right now, else the current thread."""
    try:
        task = asyncio.current_task()
    except RuntimeError:  # no running loop in this thread
        task = None
    return ("task", id(task)) if task is not None else ("thread", threading.get_ident())


@event.listens_for(Session, "after_begin")
def _track_session(session: Session, _transaction, _connection) -> None:
    session.info["owner"] = _owner()
    _live.add(session)


def holds_write_lock(session: Session) -> bool:
    """True if this session has an open SQLite write transaction.

    Python's sqlite3 only issues BEGIN right before a write, so an open DB-API
    transaction means this session holds (or is queued for) the write lock."""
    if not session.in_transaction():
        return False
    dbapi = session.connection().connection.dbapi_connection
    return bool(getattr(dbapi, "in_transaction", False))


def release_write_lock(reason: str = "") -> int:
    """Commit any write transaction held by the current task before it waits on the network.

    Call this before an await that can take a while (HTTP, Discord). Sessions belonging to
    other tasks or threads are never touched. Returns how many sessions were committed."""
    me = _owner()
    committed = 0
    for session in list(_live):
        if session.info.get("owner") != me or not holds_write_lock(session):
            continue
        log.debug("Committing before network wait%s", f" ({reason})" if reason else "")
        session.commit()
        committed += 1
    return committed


# ------------------------------------------------------------------ busy handling

def is_locked_error(exc: BaseException) -> bool:
    """SQLite 'database is locked' / 'busy', whether raw or wrapped by SQLAlchemy."""
    orig = exc.orig if isinstance(exc, OperationalError) else exc
    if not isinstance(orig, sqlite3.OperationalError):
        return False
    msg = str(orig).lower()
    return "locked" in msg or "busy" in msg


def with_retry(fn: Callable[[], T], *, attempts: int = 3, delay: float = 0.5,
               what: str = "database write") -> T:
    """Run a self-contained DB unit of work (it opens and commits its own session),
    retrying a few times if SQLite is busy. Blocking: call it from a worker thread
    (run_in_threadpool / asyncio.to_thread), never directly on the event loop."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except OperationalError as e:
            if not is_locked_error(e) or attempt == attempts:
                raise
            log.warning("Database busy during %s; retrying (%d/%d)", what, attempt, attempts - 1)
            time.sleep(delay * attempt)
    raise AssertionError("unreachable")


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
