"""Courier / jump-freight contract tracking."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow

# ESI contract statuses
OPEN_STATUSES = {"outstanding", "in_progress"}
CLOSED_STATUSES = {"finished_issuer", "finished_contractor", "finished", "cancelled",
                   "rejected", "failed", "deleted", "reversed"}

# Your internal workflow, independent of ESI
WORK_STATUSES = ["new", "queued", "accepted", "in_transit", "delivered", "rejected"]


class Customer(Base):
    __tablename__ = "customers"

    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(64))
    corporation_id: Mapped[int | None] = mapped_column(BigInteger)
    corporation_name: Mapped[str | None] = mapped_column(String(64))
    tag: Mapped[str | None] = mapped_column(String(32))   # vip, alliance, ...
    notes: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # Abuse controls. muted = tracked but no DMs; blocked = no DMs, hidden, no lookups.
    muted: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    flag_reason: Mapped[str | None] = mapped_column(Text)
    flagged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def silenced(self) -> bool:
        return bool(self.muted or self.blocked)

    contracts: Mapped[list["Contract"]] = relationship(back_populates="customer")


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (
        Index("ix_contracts_status_issued", "status", "date_issued"),
        Index("ix_contracts_issuer", "issuer_id"),
    )

    contract_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    corporation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("corporations.corporation_id"))
    issuer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("customers.character_id"))
    issuer_corp_id: Mapped[int | None] = mapped_column(BigInteger)
    acceptor_id: Mapped[int | None] = mapped_column(BigInteger)
    assignee_id: Mapped[int | None] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))
    title: Mapped[str | None] = mapped_column(String(256))
    start_location_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("locations.location_id"))
    end_location_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("locations.location_id"))
    volume_m3: Mapped[float | None] = mapped_column(Float)
    reward_isk: Mapped[float | None] = mapped_column(Float)
    collateral_isk: Mapped[float | None] = mapped_column(Float)
    # Item exchange only: what the acceptor pays (0 for "haul this for me" bulk contracts).
    price_isk: Mapped[float | None] = mapped_column(Float)
    # Bulk (item exchange) contracts belong to a job; their goods' Jita buy value is
    # fixed when first priced, so later price moves don't rewrite history.
    job_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bulk_jobs.id", ondelete="SET NULL"), index=True)
    value_isk: Mapped[float | None] = mapped_column(Float)
    days_to_complete: Mapped[int | None] = mapped_column(Integer)
    date_issued: Mapped[datetime] = mapped_column(UTCDateTime)
    date_expired: Mapped[datetime | None] = mapped_column(UTCDateTime)
    date_accepted: Mapped[datetime | None] = mapped_column(UTCDateTime)
    date_completed: Mapped[datetime | None] = mapped_column(UTCDateTime)

    work_status: Mapped[str] = mapped_column(String(16), default="new")
    notes: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    raw_json: Mapped[str | None] = mapped_column(Text)

    customer: Mapped[Customer] = relationship(back_populates="contracts")
    start_location = relationship("Location", foreign_keys=[start_location_id], lazy="joined")
    end_location = relationship("Location", foreign_keys=[end_location_id], lazy="joined")
    events: Mapped[list["ContractEvent"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan",
        order_by="ContractEvent.observed_at.desc()")

    items: Mapped[list["ContractItem"]] = relationship(
        back_populates="contract", cascade="all, delete-orphan", order_by="ContractItem.record_id")
    job: Mapped["BulkJob | None"] = relationship(back_populates="contracts")

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def is_bulk(self) -> bool:
        return self.type == "item_exchange"

    @property
    def deadline(self) -> datetime | None:
        """When an accepted courier must be delivered."""
        if self.date_accepted and self.days_to_complete:
            from datetime import timedelta
            return self.date_accepted + timedelta(days=self.days_to_complete)
        return None


class ContractEvent(Base):
    __tablename__ = "contract_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contracts.contract_id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(8))  # esi | user
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    contract: Mapped[Contract] = relationship(back_populates="events")


class ContractItem(Base):
    """What's in a contract (ESI /contracts/{id}/items), kept so nothing is lost even after
    the contract itself is gone."""
    __tablename__ = "contract_items"

    record_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    contract_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("contracts.contract_id", ondelete="CASCADE"), index=True)
    type_id: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(BigInteger)
    is_included: Mapped[bool] = mapped_column(Boolean, default=True)   # False = asked for
    is_bpc: Mapped[bool] = mapped_column(Boolean, default=False)
    unit_m3: Mapped[float | None] = mapped_column(Float)      # packaged volume, if known
    unit_value: Mapped[float | None] = mapped_column(Float)   # Jita buy when priced

    contract: Mapped[Contract] = relationship(back_populates="items")


class BulkPlan(Base):
    """A saved trip plan for a bulk job: JSON trips and steps, and which steps are done.
    `fingerprint` is what it was planned from (contracts, destination, pilots); when the
    job no longer matches, the plan is shown as out of date."""
    __tablename__ = "bulk_plans"

    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bulk_jobs.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[str] = mapped_column(Text)
    done: Mapped[str] = mapped_column(Text, default="[]")
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    job: Mapped["BulkJob"] = relationship(back_populates="plan")


# Bulk job lifecycle (ours; ESI's item-exchange status flips to "finished" the moment the
# corp accepts, which here means "in our hands", not delivered).
JOB_STATUSES = ["new", "accepted", "hauling", "delivered", "closed"]


class BulkJob(Base):
    """A batch of bulk-haul (item exchange) contracts from one requester, arriving together.
    Paid upfront (ISK on the contracts) or priced after (0 ISK, reward agreed later)."""
    __tablename__ = "bulk_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issuer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("customers.character_id"),
                                           index=True)
    corporation_id: Mapped[int] = mapped_column(BigInteger)
    title: Mapped[str | None] = mapped_column(String(128))
    first_issued_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_issued_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(16), default="new")
    # Destination hub: nearest by default; dest_override marks a manual choice.
    dest_system_id: Mapped[int | None] = mapped_column(BigInteger)
    dest_override: Mapped[bool] = mapped_column(Boolean, default=False)
    # "Price after" jobs: what was quoted / actually paid, entered later (Batch B watches
    # the return contract).
    agreed_reward_isk: Mapped[float | None] = mapped_column(Float)
    return_collateral_isk: Mapped[float | None] = mapped_column(Float)
    priced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # value snapshot time
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # The contract the corp issued back to the requester (spotted automatically).
    return_contract_id: Mapped[int | None] = mapped_column(BigInteger)
    return_type: Mapped[str | None] = mapped_column(String(16))
    return_status: Mapped[str | None] = mapped_column(String(24))
    return_price_isk: Mapped[float | None] = mapped_column(Float)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    customer: Mapped[Customer] = relationship()
    plan: Mapped["BulkPlan | None"] = relationship(back_populates="job", uselist=False,
                                                   cascade="all, delete-orphan")
    contracts: Mapped[list[Contract]] = relationship(back_populates="job",
                                                     order_by="Contract.date_issued")

    @property
    def live(self) -> list[Contract]:
        """Contracts still part of the job: not deleted, cancelled, rejected or expired
        unaccepted. Totals and value count these only."""
        return [c for c in self.contracts if not is_withdrawn(c)]

    @property
    def withdrawn(self) -> list[Contract]:
        return [c for c in self.contracts if is_withdrawn(c)]

    @property
    def waiting(self) -> list[Contract]:
        """Not accepted yet and still acceptable."""
        return [c for c in self.live if c.status == "outstanding"]

    @property
    def upfront_isk(self) -> float:
        return sum(c.reward_isk or 0 for c in self.live)

    @property
    def price_mode(self) -> str:
        """'upfront': the contracts pay ISK. 'after': 0 ISK, you set the reward later."""
        return "upfront" if self.upfront_isk > 0 else "after"

    @property
    def m3(self) -> float:
        return sum(c.volume_m3 or 0 for c in self.live)

    @property
    def value_isk(self) -> float:
        return sum(c.value_isk or 0 for c in self.live)

    @property
    def exposure_isk(self) -> float:
        """Collateral you've committed to if a return contract is set, else the goods'
        value while they're in your hands (accepted, not yet delivered)."""
        if self.status in ("delivered", "closed"):
            return 0.0
        held = sum(c.value_isk or 0 for c in self.live if c.status in FINISHED_EX)
        if self.return_collateral_isk and held:
            return self.return_collateral_isk
        return held

    @property
    def earned_isk(self) -> float:
        return self.upfront_isk if self.price_mode == "upfront" else (self.agreed_reward_isk or 0)


# An item exchange the corp accepted shows as finished: the goods are ours to haul.
FINISHED_EX = ("finished", "finished_contractor", "finished_issuer")
WITHDRAWN_EX = ("deleted", "cancelled", "rejected", "reversed", "failed")


def is_withdrawn(c: Contract) -> bool:
    """Gone without us taking the goods: withdrawn by the issuer, or expired unaccepted
    (ESI keeps showing those as 'outstanding' past their expiry date)."""
    if c.status in WITHDRAWN_EX:
        return True
    return bool(c.status == "outstanding" and c.date_expired and c.date_expired <= utcnow())
