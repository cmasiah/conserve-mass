"""map_gates: stargate connections for the Dotlan-style route map

Revision ID: 0013_map_gates
Revises: 0012_jump_t2_expanders
Create Date: 2026-11-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_map_gates"
down_revision = "0012_jump_t2_expanders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "map_gates" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "map_gates",
            sa.Column("from_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("to_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        )
        op.create_index("ix_map_gates_to_id", "map_gates", ["to_id"])


def downgrade() -> None:
    op.drop_table("map_gates")
