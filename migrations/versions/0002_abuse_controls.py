"""abuse controls: mute/block issuers, digest-friendly notifications

Revision ID: 0002_abuse_controls
Revises: 0001_initial
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_abuse_controls"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("customers") as b:
        b.add_column(sa.Column("muted", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("blocked", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("flag_reason", sa.Text))
        b.add_column(sa.Column("flagged_at", sa.DateTime))
    with op.batch_alter_table("notifications") as b:
        b.add_column(sa.Column("line", sa.Text))
        b.add_column(sa.Column("subject_id", sa.BigInteger))
        b.create_index("ix_notifications_subject_id", ["subject_id"])
    # The old free-text "blocked" tag becomes a real block.
    op.execute("UPDATE customers SET blocked = 1 WHERE lower(tag) = 'blocked'")


def downgrade() -> None:
    with op.batch_alter_table("notifications") as b:
        b.drop_index("ix_notifications_subject_id")
        b.drop_column("subject_id")
        b.drop_column("line")
    with op.batch_alter_table("customers") as b:
        b.drop_column("flagged_at")
        b.drop_column("flag_reason")
        b.drop_column("blocked")
        b.drop_column("muted")
