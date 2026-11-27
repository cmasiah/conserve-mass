"""Industry: character skills, industry jobs, blueprints."""
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, utcnow


class CharacterSkill(Base):
    """One row per (character, skill). Names come from the shared entity_names cache."""

    __tablename__ = "character_skills"

    character_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("characters.character_id", ondelete="CASCADE"), primary_key=True)
    skill_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    trained_level: Mapped[int] = mapped_column(Integer, default=0)
    active_level: Mapped[int] = mapped_column(Integer, default=0)  # alpha clones can be lower
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class IndustryJob(Base):
    """Active / ready jobs only; rows disappear once ESI stops listing them."""

    __tablename__ = "industry_jobs"

    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_type: Mapped[str] = mapped_column(String(12))       # character | corporation
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    installer_id: Mapped[int] = mapped_column(BigInteger, index=True)
    activity_id: Mapped[int] = mapped_column(Integer)
    blueprint_type_id: Mapped[int] = mapped_column(BigInteger)
    product_type_id: Mapped[int | None] = mapped_column(BigInteger)
    runs: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16))           # active | ready | paused ...
    location_id: Mapped[int | None] = mapped_column(BigInteger)
    facility_id: Mapped[int | None] = mapped_column(BigInteger)
    start_date: Mapped[datetime] = mapped_column(UTCDateTime)
    end_date: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Blueprint(Base):
    __tablename__ = "blueprints"

    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_type: Mapped[str] = mapped_column(String(12))       # character | corporation
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    type_id: Mapped[int] = mapped_column(BigInteger, index=True)
    location_id: Mapped[int] = mapped_column(BigInteger)
    location_flag: Mapped[str] = mapped_column(String(32))    # e.g. CorpSAG1..7, Hangar
    quantity: Mapped[int] = mapped_column(Integer)            # -1 original, -2 copy, >0 stack
    material_efficiency: Mapped[int] = mapped_column(Integer, default=0)
    time_efficiency: Mapped[int] = mapped_column(Integer, default=0)
    runs: Mapped[int] = mapped_column(Integer, default=-1)    # -1 = infinite (original)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    @property
    def is_copy(self) -> bool:
        return self.quantity == -2


class TypeInfo(Base):
    """Cached ESI type → group lookup, used to decide which industry jobs to alert on."""

    __tablename__ = "type_info"

    type_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str | None] = mapped_column(String(128))
    group_id: Mapped[int | None] = mapped_column(BigInteger)
    group_name: Mapped[str | None] = mapped_column(String(128))
    category_id: Mapped[int | None] = mapped_column(BigInteger)
    # Market group, for sorting blueprints into categories. NULL = not looked up yet,
    # 0 = the type has none (e.g. some faction BPCs).
    market_group_id: Mapped[int | None] = mapped_column(BigInteger)
    resolved_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class MarketGroup(Base):
    """Cached /markets/groups tree (names + parents). Static game data, fetched once."""

    __tablename__ = "market_groups"

    market_group_id: Mapped[int] = mapped_column(BigInteger, primary_key=True,
                                                 autoincrement=False)
    name: Mapped[str] = mapped_column(String(128))
    parent_id: Mapped[int | None] = mapped_column(BigInteger)


class AssetNode(Base):
    """A place inside an owner's assets that holds other items: a corp office, a
    container, a ship. Only what's needed to say which structure a blueprint is in."""

    __tablename__ = "asset_nodes"

    item_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_type: Mapped[str] = mapped_column(String(12))
    owner_id: Mapped[int] = mapped_column(BigInteger, index=True)
    parent_id: Mapped[int] = mapped_column(BigInteger)        # where this node sits
    parent_flag: Mapped[str] = mapped_column(String(32), default="")
    type_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(String(128))     # container's in-game name
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SkillQueue(Base):
    """Per-character skill queue summary (not every entry; Home only needs the gist)."""

    __tablename__ = "skill_queues"

    character_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("characters.character_id", ondelete="CASCADE"), primary_key=True)
    length: Mapped[int] = mapped_column(Integer, default=0)
    current_skill_id: Mapped[int | None] = mapped_column(BigInteger)
    current_level: Mapped[int | None] = mapped_column(Integer)
    current_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    queue_end: Mapped[datetime | None] = mapped_column(UTCDateTime)  # None + length>0 = paused
    # JSON {skill_id: highest level queued}, for showing queued levels on the Slots page
    queued: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
