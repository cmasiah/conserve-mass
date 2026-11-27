"""type_info cache for industry job alerts

Revision ID: 0004_type_info
Revises: 0003_industry
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0004_type_info"
down_revision = "0003_industry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "type_info" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "type_info",
        sa.Column("type_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(128)),
        sa.Column("group_id", sa.BigInteger),
        sa.Column("group_name", sa.String(128)),
        sa.Column("category_id", sa.BigInteger),
        sa.Column("resolved_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("type_info")
