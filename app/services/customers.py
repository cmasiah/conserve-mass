"""Repeat-customer statistics."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Contract, Customer

FINISHED = ("finished", "finished_contractor", "finished_issuer")


@dataclass
class CustomerStats:
    customer: Customer
    total: int
    completed: int
    failed: int
    isk_earned: float
    total_m3: float
    total_collateral: float
    last_contract_at: datetime | None


def customer_stats(db: Session, character_id: int | None = None,
                   order: str = "total") -> list[CustomerStats]:
    q = (
        select(
            Customer,
            func.count(Contract.contract_id),
            func.coalesce(func.sum(case((Contract.status.in_(FINISHED), 1), else_=0)), 0),
            func.coalesce(func.sum(case((Contract.status == "failed", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Contract.status.in_(FINISHED), Contract.reward_isk),
                                        else_=0)), 0),
            func.coalesce(func.sum(Contract.volume_m3), 0),
            func.coalesce(func.sum(Contract.collateral_isk), 0),
            func.max(Contract.date_issued),
        )
        .outerjoin(Contract, Contract.issuer_id == Customer.character_id)
        .group_by(Customer.character_id)
    )
    if character_id is not None:
        q = q.where(Customer.character_id == character_id)
    rows = [CustomerStats(c, t, done, f, isk, m3, coll, last)
            for c, t, done, f, isk, m3, coll, last in db.execute(q).all()]
    key = {
        "total": lambda s: s.total,
        "isk": lambda s: s.isk_earned,
        "recent": lambda s: s.last_contract_at.timestamp() if s.last_contract_at else 0,
    }.get(order, lambda s: s.total)
    rows.sort(key=key, reverse=True)
    return rows
