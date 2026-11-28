"""In-memory record of ESI calls from the last hour, for the System page.

Answers "is this app hammering ESI?" with real numbers instead of guesses: calls per
feed, how many were cheap 304s, errors, and the lowest rate-limit headroom seen.

How CCP counts (developers.eveonline.com, ESI rate limiting):
  * each rate-limit group has a bucket per application + CHARACTER for authenticated
    routes, so 30 characters get 30 separate buckets; public routes share one per IP;
  * 2xx costs 2 tokens, 304 costs 1, 4xx costs 5, 5xx costs 0;
  * routes without the new limits fall under the old rule: at most 100 error
    responses per minute per IP, then everything returns 420.
"""
import re
import time
from collections import Counter, deque
from dataclasses import dataclass

WINDOW_S = 3600

_FAMILIES = [
    (re.compile(r"^/corporations/\d+/contracts"), "Corp contracts"),
    (re.compile(r"^/characters/\d+/skillqueue"), "Skill queues"),
    (re.compile(r"^/characters/\d+/skills"), "Skills"),
    (re.compile(r"^/characters/\d+/industry/jobs"), "Personal jobs"),
    (re.compile(r"^/corporations/\d+/industry/jobs"), "Corp jobs"),
    (re.compile(r"^/characters/\d+/blueprints"), "Personal blueprints"),
    (re.compile(r"^/corporations/\d+/blueprints"), "Corp blueprints"),
    (re.compile(r"^/characters/\d+/assets"), "Personal assets"),
    (re.compile(r"^/corporations/\d+/assets"), "Corp assets"),
    (re.compile(r"^/corporations/\d+/divisions"), "Corp divisions"),
    (re.compile(r"^/universe/names"), "Name lookups"),
    (re.compile(r"^/universe/(types|groups)/"), "Item type lookups"),
    (re.compile(r"^/markets/groups/"), "Market group lookups"),
    (re.compile(r"^/universe/(stations|structures|systems)/"), "Location lookups"),
    (re.compile(r"^/characters/\d+$"), "Character info"),
    (re.compile(r"^/corporations/\d+$"), "Corp info"),
]


def family(path: str) -> str:
    for rx, name in _FAMILIES:
        if rx.search(path):
            return name
    return "Other"


@dataclass
class Call:
    at: float
    family: str
    status: int
    group: str | None
    remaining: int | None
    limit: str | None


_calls: deque[Call] = deque(maxlen=50_000)


@dataclass
class Failure:
    at: float
    method: str
    path: str
    family: str
    status: int
    message: str


_failures: deque[Failure] = deque(maxlen=50)

# What each status usually means here, in plain words.
STATUS_HINTS = {
    400: "Bad request: the app asked for something ESI didn't accept.",
    401: "Not signed in: the character's login expired or was revoked. Re-add it on Characters.",
    403: "Forbidden: the character lacks the in-game role (e.g. Director) or the scope for this.",
    404: "Not found: the item, structure or character no longer exists or isn't visible.",
    420: "Error limit hit: too many errors in a minute; the app pauses until it resets.",
    429: "Rate limited: this character's bucket ran dry; the app waits and retries.",
    500: "ESI server error on CCP's side; it retries on the next sync.",
    502: "ESI is unreachable (CCP gateway); usually passes on its own.",
    503: "ESI is down or in maintenance (often around downtime, 11:00 UTC).",
    504: "ESI timed out on CCP's side; usually passes on its own.",
}


def _message(body: str) -> str:
    """ESI errors are JSON like {"error": "..."}; keep just the text, short."""
    import json
    text = (body or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            text = str(data.get("error") or data.get("message") or text)
    except ValueError:
        pass
    return " ".join(text.split())[:240]


def record(path: str, status: int, headers, method: str = "GET", body: str = "") -> None:
    if status >= 400:
        _failures.append(Failure(time.time(), method, path, family(path), status, _message(body)))
    rem = headers.get("X-Ratelimit-Remaining")
    _calls.append(Call(time.time(), family(path), status, headers.get("X-Ratelimit-Group"),
                       int(rem) if rem and rem.isdigit() else None,
                       headers.get("X-Ratelimit-Limit")))


def reset() -> None:
    _calls.clear()
    _failures.clear()


def recent_failures(limit: int = 20) -> list[dict]:
    """Newest first: when, what, the status, ESI's own message and what it usually means."""
    from datetime import datetime, timezone
    return [{"at": datetime.fromtimestamp(f.at, timezone.utc), "method": f.method, "path": f.path, "family": f.family,
             "status": f.status, "message": f.message,
             "hint": STATUS_HINTS.get(f.status, "")}
            for f in reversed(list(_failures)[-limit:])]


def _recent(now: float | None = None) -> list[Call]:
    cutoff = (now or time.time()) - WINDOW_S
    while _calls and _calls[0].at < cutoff:
        _calls.popleft()
    return list(_calls)


def summary(now: float | None = None) -> dict:
    calls = _recent(now)
    by_family: dict[str, Counter] = {}
    for c in calls:
        k = ("fresh" if 200 <= c.status < 300 else "cached" if c.status == 304
             else "error" if c.status >= 400 else "other")
        by_family.setdefault(c.family, Counter())[k] += 1
    rows = sorted(({"family": f, "total": sum(n.values()), **n} for f, n in by_family.items()),
                  key=lambda r: -r["total"])
    # Tightest rate-limit bucket seen (lowest share remaining), per group.
    tight: dict[str, tuple[int, str]] = {}
    for c in calls:
        if c.group and c.remaining is not None and c.limit:
            if c.group not in tight or c.remaining < tight[c.group][0]:
                tight[c.group] = (c.remaining, c.limit)
    errors_last_min = sum(1 for c in calls if c.status >= 400 and c.at >= (now or time.time()) - 60)
    return {
        "total": len(calls),
        "fresh": sum(r.get("fresh", 0) for r in rows),
        "cached": sum(r.get("cached", 0) for r in rows),
        "errors": sum(r.get("error", 0) for r in rows),
        "errors_last_min": errors_last_min,
        "rows": rows,
        "tight": sorted(({"group": g, "remaining": r, "limit": lim}
                         for g, (r, lim) in tight.items()),
                        key=lambda x: x["remaining"])[:5],
        "since_start": len(calls) < _calls.maxlen,
        "failures": recent_failures(),
    }
