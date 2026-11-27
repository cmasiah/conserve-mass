from datetime import datetime, timezone


def fmt_isk(value: float | None) -> str:
    if value is None:
        return "—"
    v = float(value)
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"{v / div:,.2f}{suffix}"
    return f"{v:,.0f}"


def fmt_m3(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f} m³"


def fmt_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M EVE")  # EVE time = UTC


def fmt_delta(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return "—"
    now = now or datetime.now(timezone.utc)
    secs = int((value - now).total_seconds())
    past = secs < 0
    secs = abs(secs)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    text = f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")
    return f"{text} ago" if past else f"in {text}"
