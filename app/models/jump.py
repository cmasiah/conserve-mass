"""Jump freighter planning: the star map, pilots' JF setups and cyno points."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime, utcnow


class MapSystem(Base):
    """A solar system's position (meters) and security, from ESI. Static; fetched once."""

    __tablename__ = "map_systems"

    system_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(64), index=True)
    constellation_id: Mapped[int] = mapped_column(BigInteger)
    region_id: Mapped[int | None] = mapped_column(BigInteger)
    region_name: Mapped[str | None] = mapped_column(String(64))
    security: Mapped[float] = mapped_column(Float)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    z: Mapped[float] = mapped_column(Float)


class JumpPilot(Base):
    """One character's jump freighter setup. Detected from skills and assets; any value
    can be overridden with the real number from the in-game fitting window."""

    __tablename__ = "jump_pilots"

    character_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ship_type_id: Mapped[int | None] = mapped_column(BigInteger)
    economizers: Mapped[int | None] = mapped_column(Integer)     # None = read from the fit
    expanders: Mapped[int | None] = mapped_column(Integer)      # ORE Expanded Cargoholds
    t2_expanders: Mapped[int | None] = mapped_column(Integer)   # Expanded Cargohold IIs
    range_override: Mapped[float | None] = mapped_column(Float)   # LY
    fuel_override: Mapped[float | None] = mapped_column(Float)    # isotopes per LY
    cargo_override: Mapped[float | None] = mapped_column(Float)   # m3
    max_collateral: Mapped[float | None] = mapped_column(Float)   # ISK per trip
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CynoPoint(Base):
    """A system you can light a cyno in (a cyno alt or beacon)."""

    __tablename__ = "cyno_points"

    system_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    label: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
