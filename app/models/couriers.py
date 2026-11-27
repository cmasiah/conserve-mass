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

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

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
