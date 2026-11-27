"""abuse controls: mute/block issuers, digest-friendly notifications

Revision ID: 0002_abuse_controls
Revises: 0001_initial
Create Date: 2026-11-27

Uses plain ALTER TABLE ADD COLUMN (SQLite supports it) instead of a batch table
rebuild: a rebuild of `customers` fails while contracts reference it, and the
first 0.4.0 attempt left a `_alembic_tmp_customers` table behind. This version
cleans that up and only adds what's missing, so it is safe to re-run.
"""
import sqlalchemy as sa
from alembic import op

revision = "0002_abuse_controls"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # Leftovers from the failed 0.4.0 batch migration.
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_customers")
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_notifications")

    have = _columns("customers")
    if "muted" not in have:
        op.add_column("customers", sa.Column("muted", sa.Boolean, nullable=False,
                                             server_default=sa.false()))
    if "blocked" not in have:
        op.add_column("customers", sa.Column("blocked", sa.Boolean, nullable=False,
                                             server_default=sa.false()))
    if "flag_reason" not in have:
        op.add_column("customers", sa.Column("flag_reason", sa.Text))
    if "flagged_at" not in have:
        op.add_column("customers", sa.Column("flagged_at", sa.DateTime))

    have = _columns("notifications")
    if "line" not in have:
        op.add_column("notifications", sa.Column("line", sa.Text))
    if "subject_id" not in have:
        op.add_column("notifications", sa.Column("subject_id", sa.BigInteger))
    if "ix_notifications_subject_id" not in _indexes("notifications"):
        op.create_index("ix_notifications_subject_id", "notifications", ["subject_id"])

    # The old free-text "blocked" tag becomes a real block.
    op.execute("UPDATE customers SET blocked = 1 WHERE lower(tag) = 'blocked'")


def downgrade() -> None:
    op.drop_index("ix_notifications_subject_id", table_name="notifications")
    for table, col in [("notifications", "subject_id"), ("notifications", "line"),
                       ("customers", "flagged_at"), ("customers", "flag_reason"),
                       ("customers", "blocked"), ("customers", "muted")]:
        op.drop_column(table, col)  # SQLite >= 3.35
