"""Jita 4-4 prices for valuing cargo (bulk jobs, collateral) and fuel.

Source: Fuzzwork's market aggregates (one request covers hundreds of item types, no key).
"Jita buy" is the top 5% of buy orders by volume (what you'd get selling now, without one
odd high order skewing it). If Fuzzwork can't be reached, CCP's universe-wide average
price is used instead and marked as an estimate.

Prices are cached in market_prices for about an hour.
"""
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import session_scope, utcnow
from app.esi import client as esi
from app.models import MarketPrice

log = logging.getLogger(__name__)

JITA_STATION = 60003760
FUZZWORK_URL = "https://market.fuzzwork.co.uk/aggregates/"
MAX_AGE = timedelta(hours=1)
CHUNK = 200


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def parse_fuzzwork(data: dict) -> dict[int, tuple[float | None, float | None]]:
    """{type_id: (jita_buy, jita_sell)} from Fuzzwork's aggregates JSON."""
    out = {}
    for k, v in (data or {}).items():
        try:
            tid = int(k)
        except ValueError:
            continue
        buy, sell = (v or {}).get("buy") or {}, (v or {}).get("sell") or {}
        out[tid] = (_num(buy.get("percentile")) or _num(buy.get("max")),
                    _num(sell.get("percentile")) or _num(sell.get("min")))
    return out


async def _fuzzwork(type_ids: list[int]) -> dict[int, tuple]:
    out = {}
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "conserve-mass-ops"}) as c:
        for i in range(0, len(type_ids), CHUNK):
            chunk = type_ids[i:i + CHUNK]
            r = await c.get(FUZZWORK_URL, params={"station": JITA_STATION,
                                                  "types": ",".join(map(str, chunk))})
            r.raise_for_status()
            out.update(parse_fuzzwork(r.json()))
    return out


async def _esi_average() -> dict[int, tuple]:
    rows = await esi.get("/markets/prices")
    return {r["type_id"]: (_num(r.get("average_price")), None) for r in rows or []}


async def ensure(type_ids: set[int]) -> None:
    """Refresh prices that are missing or older than an hour. Network first, then one
    short write (no transaction held across the awaits)."""
    with session_scope() as db:
        fresh = set(db.scalars(select(MarketPrice.type_id).where(
            MarketPrice.type_id.in_(type_ids), MarketPrice.updated_at >= utcnow() - MAX_AGE)))
    need = sorted(set(type_ids) - fresh)
    if not need:
        return
    source = "fuzzwork"
    try:
        got = await _fuzzwork(need)
    except Exception as e:  # noqa: BLE001 — fall back to CCP's averages
        log.info("Jita prices unavailable from Fuzzwork (%s); using ESI averages", e)
        source, got = "esi", {}
        try:
            allp = await _esi_average()
            got = {t: allp[t] for t in need if t in allp}
        except Exception as e2:  # noqa: BLE001
            log.warning("No prices available: %s", e2)
            return
    now = utcnow()
    with session_scope() as db:
        for tid in need:
            buy, sell = got.get(tid, (None, None))
            db.merge(MarketPrice(type_id=tid, jita_buy=buy, jita_sell=sell, source=source,
                                 updated_at=now))
        db.commit()


def jita_buy(db: Session, type_ids: set[int]) -> dict[int, float]:
    return {p.type_id: p.jita_buy for p in db.scalars(
        select(MarketPrice).where(MarketPrice.type_id.in_(type_ids))) if p.jita_buy}


def source_of(db: Session, type_ids: set[int]) -> str:
    """'fuzzwork', 'esi' (estimate) or '' when nothing is priced."""
    srcs = set(db.scalars(select(MarketPrice.source).where(MarketPrice.type_id.in_(type_ids))))
    return "esi" if "esi" in srcs else ("fuzzwork" if srcs else "")
