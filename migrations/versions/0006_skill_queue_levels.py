"""skill_queues.queued: which skill levels are in each queue

Revision ID: 0006_skill_queue_levels
Revises: 0005_skill_queue
Create Date: 2026-11-27

ADD COLUMN only (no table rebuild), and skipped if it already exists.
"""
import sqlalchemy as sa
from alembic import op

revision = "0006_skill_queue_levels"
down_revision = "0005_skill_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("skill_queues")}
    if "queued" not in cols:
        op.add_column("skill_queues", sa.Column("queued", sa.Text()))


def downgrade() -> None:
    pass  # leaving an unused column is harmless; SQLite can't drop it without a rebuild
