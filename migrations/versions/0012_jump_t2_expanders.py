"""jump_pilots.t2_expanders: tell Expanded Cargohold IIs apart from ORE ones

Revision ID: 0012_jump_t2_expanders
Revises: 0011_jump_planner
Create Date: 2026-11-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_jump_t2_expanders"
down_revision = "0011_jump_planner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("jump_pilots")}
    if "t2_expanders" not in cols:
        op.add_column("jump_pilots", sa.Column("t2_expanders", sa.Integer()))


def downgrade() -> None:
    pass
