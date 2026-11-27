"""Customer statistics. A "customer" is someone with at least one delivered contract;
issuers whose contracts were only cancelled/deleted/expired are kept in the DB
(contracts reference them) but hidden from the Customers tab by default."""
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models import Contract, Customer

FINISHED = ("finished", "finished_contractor", "finished_issuer")
WITHDRAWN = ("cancelled", "deleted", "rejected", "reversed")


@dataclass
class CustomerStats:
    customer: Customer
    total: int            # every contract they've issued to us
    completed: int        # delivered
    failed: int
    withdrawn: int        # cancelled / deleted / rejected by the issuer
    isk_earned: float     # reward from delivered contracts
    total_m3: float       # volume delivered
    total_collateral: float  # collateral carried on delivered contracts
    last_completed_at: datetime | None
    last_contract_at: datetime | None


def _done(expr):
    return case((Contract.status.in_(FINISHED), expr), else_=0)


def customer_stats(db: Session, character_id: int | None = None, order: str = "completed",
                   completed_only: bool = True) -> list[CustomerStats]:
    completed = func.coalesce(func.sum(_done(1)), 0)
    q = (
        select(
            Customer,
            func.count(Contract.contract_id),
            completed,
            func.coalesce(func.sum(case((Contract.status == "failed", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Contract.status.in_(WITHDRAWN), 1), else_=0)), 0),
            func.coalesce(func.sum(_done(Contract.reward_isk)), 0),
            func.coalesce(func.sum(_done(Contract.volume_m3)), 0),
            func.coalesce(func.sum(_done(Contract.collateral_isk)), 0),
            func.max(case((Contract.status.in_(FINISHED), Contract.date_completed))),
            func.max(Contract.date_issued),
        )
        .outerjoin(Contract, Contract.issuer_id == Customer.character_id)
        .group_by(Customer.character_id)
    )
    if character_id is not None:
        q = q.where(Customer.character_id == character_id)
    elif completed_only:
        q = q.having(completed >= 1)

    rows = [CustomerStats(*r) for r in db.execute(q).all()]
    key = {
        "completed": lambda s: (s.completed, s.isk_earned),
        "isk": lambda s: s.isk_earned,
        "recent": lambda s: (s.last_completed_at or s.last_contract_at).timestamp()
        if (s.last_completed_at or s.last_contract_at) else 0,
    }.get(order, lambda s: (s.completed, s.isk_earned))
    rows.sort(key=key, reverse=True)
    return rows
