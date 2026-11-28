"""skill queue summary for the Home page

Revision ID: 0005_skill_queue
Revises: 0004_type_info
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0005_skill_queue"
down_revision = "0004_type_info"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "skill_queues" in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        "skill_queues",
        sa.Column("character_id", sa.BigInteger,
                  sa.ForeignKey("characters.character_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("length", sa.Integer, nullable=False, server_default="0"),
        sa.Column("current_skill_id", sa.BigInteger),
        sa.Column("current_level", sa.Integer),
        sa.Column("current_end", sa.DateTime()),
        sa.Column("queue_end", sa.DateTime()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("skill_queues")
