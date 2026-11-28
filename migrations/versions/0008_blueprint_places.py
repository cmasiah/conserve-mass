"""Blueprint structures and categories: asset_nodes, market_groups, type_info.market_group_id

Revision ID: 0008_blueprint_places
Revises: 0007_character_tracking
Create Date: 2026-11-27

New tables + ADD COLUMN only; every step is skipped if already done.
"""
import sqlalchemy as sa
from alembic import op

revision = "0008_blueprint_places"
down_revision = "0007_character_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if "type_info" in tables and "market_group_id" not in {
            c["name"] for c in insp.get_columns("type_info")}:
        op.add_column("type_info", sa.Column("market_group_id", sa.BigInteger()))
    if "market_groups" not in tables:
        op.create_table(
            "market_groups",
            sa.Column("market_group_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("name", sa.String(128), nullable=False),
            sa.Column("parent_id", sa.BigInteger()),
        )
    if "asset_nodes" not in tables:
        op.create_table(
            "asset_nodes",
            sa.Column("item_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("owner_type", sa.String(12), nullable=False),
            sa.Column("owner_id", sa.BigInteger(), nullable=False),
            sa.Column("parent_id", sa.BigInteger(), nullable=False),
            sa.Column("parent_flag", sa.String(32), nullable=False, server_default=""),
            sa.Column("type_id", sa.BigInteger(), nullable=False),
            sa.Column("name", sa.String(128)),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_asset_nodes_owner_id", "asset_nodes", ["owner_id"])


def downgrade() -> None:
    pass
