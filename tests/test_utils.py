from datetime import datetime, timedelta, timezone

from app.utils import fmt_delta, fmt_isk


def test_fmt_isk():
    assert fmt_isk(250_000_000) == "250.00M"
    assert fmt_isk(3_000_000_000) == "3.00B"
    assert fmt_isk(None) == "—"


def test_fmt_delta():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert fmt_delta(now + timedelta(days=2, hours=3), now) == "in 2d 3h"
    assert fmt_delta(now - timedelta(minutes=5), now) == "5m ago"
