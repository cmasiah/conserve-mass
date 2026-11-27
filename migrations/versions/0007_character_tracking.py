"""characters.track_*: per-character choice of what to sync and show

Revision ID: 0007_character_tracking
Revises: 0006_skill_queue_levels
Create Date: 2026-11-27

ADD COLUMN only (no table rebuild), each skipped if it already exists. Everything
defaults to on, so existing characters keep behaving exactly as before.
"""
import sqlalchemy as sa
from alembic import op

revision = "0007_character_tracking"
down_revision = "0006_skill_queue_levels"
branch_labels = None
depends_on = None

COLUMNS = ("track_skills", "track_slots", "track_industry", "track_assets")


def upgrade() -> None:
    cols = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("characters")}
    for name in COLUMNS:
        if name not in cols:
            op.add_column("characters", sa.Column(name, sa.Boolean(), nullable=False,
                                                  server_default=sa.true()))


def downgrade() -> None:
    pass  # unused columns are harmless; SQLite can't drop them without a rebuild
