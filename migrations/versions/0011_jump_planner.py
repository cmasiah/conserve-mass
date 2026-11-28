"""Jump planner: map_systems, jump_pilots, cyno_points

Revision ID: 0011_jump_planner
Revises: 0010_assets
Create Date: 2026-11-28

New tables only; each skipped if it already exists.
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_jump_planner"
down_revision = "0010_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "map_systems" not in tables:
        op.create_table(
            "map_systems",
            sa.Column("system_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("name", sa.String(64), nullable=False),
            sa.Column("constellation_id", sa.BigInteger(), nullable=False),
            sa.Column("region_id", sa.BigInteger()),
            sa.Column("region_name", sa.String(64)),
            sa.Column("security", sa.Float(), nullable=False),
            sa.Column("x", sa.Float(), nullable=False),
            sa.Column("y", sa.Float(), nullable=False),
            sa.Column("z", sa.Float(), nullable=False),
        )
        op.create_index("ix_map_systems_name", "map_systems", ["name"])
    if "jump_pilots" not in tables:
        op.create_table(
            "jump_pilots",
            sa.Column("character_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("ship_type_id", sa.BigInteger()),
            sa.Column("economizers", sa.Integer()),
            sa.Column("expanders", sa.Integer()),
            sa.Column("range_override", sa.Float()),
            sa.Column("fuel_override", sa.Float()),
            sa.Column("cargo_override", sa.Float()),
            sa.Column("max_collateral", sa.Float()),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if "cyno_points" not in tables:
        op.create_table(
            "cyno_points",
            sa.Column("system_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("label", sa.String(120), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )


def downgrade() -> None:
    pass
