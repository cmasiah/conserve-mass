"""assets.is_bpc; blueprint quantities from ESI's -1 / -2 codes to 1

In ESI's asset list a blueprint's quantity is a code: -1 = original, -2 = copy. Stored
as-is they summed to negative totals (and dropped out of copy lists).

Revision ID: 0014_asset_bpc
Revises: 0013_map_gates
Create Date: 2026-11-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_asset_bpc"
down_revision = "0013_map_gates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("assets")}
    if "is_bpc" not in cols:
        op.add_column("assets", sa.Column("is_bpc", sa.Boolean(), nullable=False,
                                          server_default=sa.false()))
    op.execute("UPDATE assets SET is_bpc = 1 WHERE quantity = -2")
    op.execute("UPDATE assets SET quantity = 1 WHERE quantity < 0")


def downgrade() -> None:
    pass
