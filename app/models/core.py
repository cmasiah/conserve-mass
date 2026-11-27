"""Shared tables: characters, tokens, corporations, name caches, notifications, sync state.

Industry modules (jobs, skills, stockpiles) will hang off `characters` and
`corporations` in later migrations.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, Float, ForeignKey, Integer, LargeBinary, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime, utcnow


class Character(Base):
    __tablename__ = "characters"

    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(64))
    corporation_id: Mapped[int | None] = mapped_column(BigInteger)
    alliance_id: Mapped[int | None] = mapped_column(BigInteger)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    token: Mapped["EsiToken | None"] = relationship(back_populates="character", uselist=False,
                                                    cascade="all, delete-orphan")


class EsiToken(Base):
    __tablename__ = "esi_tokens"

    character_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("characters.character_id", ondelete="CASCADE"), primary_key=True)
    refresh_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    scopes: Mapped[str] = mapped_column(Text, default="")
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    character: Mapped[Character] = relationship(back_populates="token")

    @property
    def scope_set(self) -> set[str]:
        return set(self.scopes.split())


class Corporation(Base):
    __tablename__ = "corporations"

    corporation_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(64))
    ticker: Mapped[str | None] = mapped_column(String(8))
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_character_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("characters.character_id", ondelete="SET NULL"))


class EntityName(Base):
    """Cache of /universe/names results (characters, corps, alliances, systems, types)."""

    __tablename__ = "entity_names"

    entity_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(32))
    resolved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Location(Base):
    __tablename__ = "locations"

    location_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="unknown")  # station|structure|unknown
    system_id: Mapped[int | None] = mapped_column(BigInteger)
    system_name: Mapped[str | None] = mapped_column(String(64))
    security: Mapped[float | None] = mapped_column(Float)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    @property
    def display(self) -> str:
        return self.name or f"Unknown location {self.location_id}"


class Notification(Base):
    """Outbox for Discord DMs. UNIQUE(ref, kind) guarantees no duplicate alerts."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("ref", "kind", name="uq_notification_ref_kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ref: Mapped[str] = mapped_column(String(64))          # e.g. "contract:212345678"
    kind: Mapped[str] = mapped_column(String(32))         # new_contract, expiring, token_invalid...
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(String(512))
    line: Mapped[str | None] = mapped_column(Text)        # one-line summary used in digests
    subject_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # issuer it's about
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|sent|failed|dropped
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class SyncState(Base):
    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    etag: Mapped[str | None] = mapped_column(String(256))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
