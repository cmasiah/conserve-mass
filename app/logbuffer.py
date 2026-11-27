"""Logging setup: stdout (docker logs) + in-memory ring buffer (Logs tab) +
rotating file in /config/logs (survives restarts, downloadable)."""
import itertools
import logging
import logging.handlers
from collections import deque

from app.config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
LOG_FILE_NAME = "conserve-mass.log"

# Request paths that would flood the Logs tab (health checks, the tab's own polling).
NOISY_PATHS = ("/healthz", "/logs/api", "/static/")


class _DropNoisyAccess(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name != "uvicorn.access":
            return True
        msg = record.getMessage()
        return not any(p in msg for p in NOISY_PATHS)


class _Once(logging.Filter):
    """Let a handler see each record once, even if it's attached to two loggers
    on the same propagation path."""

    def __init__(self) -> None:
        super().__init__()
        self._attr = f"_cmass_seen_{id(self)}"

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, self._attr, False):
            return False
        setattr(record, self._attr, True)
        return True


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 5000) -> None:
        super().__init__()
        self.records: deque[dict] = deque(maxlen=capacity)
        self._seq = itertools.count(1)
        self._fmt = logging.Formatter()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc = None
            if record.exc_info:
                exc = self._fmt.formatException(record.exc_info)
            elif record.exc_text:
                exc = record.exc_text
            self.records.append({
                "id": next(self._seq),
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
                "exc": exc,
            })
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def query(self, after: int = 0, min_level: str = "DEBUG", q: str = "",
              limit: int = 1000) -> list[dict]:
        self.acquire()  # same lock emit() runs under: safe snapshot
        try:
            snapshot = list(self.records)
        finally:
            self.release()
        floor = LEVELS.get(min_level.upper(), 10)
        needle = q.lower().strip()
        out = []
        for r in snapshot:
            if r["id"] <= after or LEVELS.get(r["level"], 0) < floor:
                continue
            if needle and needle not in (
                    f'{r["logger"]} {r["msg"]} {r["exc"] or ""}').lower():
                continue
            out.append(r)
        return out[-limit:]

    def counts(self) -> dict[str, int]:
        self.acquire()
        try:
            snapshot = list(self.records)
        finally:
            self.release()
        c = {"WARNING": 0, "ERROR": 0}
        for r in snapshot:
            if r["level"] in ("ERROR", "CRITICAL"):
                c["ERROR"] += 1
            elif r["level"] == "WARNING":
                c["WARNING"] += 1
        return c


ring = RingBufferHandler()


def setup_logging(settings: Settings) -> None:
    root = logging.getLogger()
    if getattr(root, "_cmass_configured", False):
        return
    root.setLevel(settings.log_level.upper())
    formatter = logging.Formatter(LOG_FORMAT)
    noise = _DropNoisyAccess()

    handlers: list[logging.Handler] = [ring]
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(noise)
        stream.addFilter(_Once())
        root.addHandler(stream)

    log_dir = settings.config_dir / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / LOG_FILE_NAME, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        fh.setFormatter(formatter)
        handlers.append(fh)
    except OSError as e:
        logging.getLogger(__name__).warning("File logging disabled: %s", e)

    for h in handlers:
        h.addFilter(noise)
        h.addFilter(_Once())
        root.addHandler(h)

    # Uvicorn's loggers don't propagate to root; attach our handlers directly.
    for name in ("uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in handlers:
            if h not in lg.handlers:
                lg.addHandler(h)
        for h in lg.handlers:
            h.addFilter(noise)  # also quiets /healthz in `docker logs`

    for name in ("httpx", "discord", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)
    root._cmass_configured = True  # type: ignore[attr-defined]
