"""Bulk job plans (saved trips you tick off) and return-contract tracking.

Revision ID: 0016_bulk_plans
Revises: 0015_bulk_jobs
Create Date: 2026-11-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_bulk_plans"
down_revision = "0015_bulk_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if "bulk_plans" not in insp.get_table_names():
        op.create_table(
            "bulk_plans",
            sa.Column("job_id", sa.Integer(), sa.ForeignKey("bulk_jobs.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("data", sa.Text(), nullable=False),
            sa.Column("done", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    cols = {c["name"] for c in insp.get_columns("bulk_jobs")}
    with op.batch_alter_table("bulk_jobs") as b:
        for name, typ in (("return_contract_id", sa.BigInteger()), ("return_type", sa.String(16)),
                          ("return_status", sa.String(24)), ("return_price_isk", sa.Float()),
                          ("paid_at", sa.DateTime())):
            if name not in cols:
                b.add_column(sa.Column(name, typ))


def downgrade() -> None:
    with op.batch_alter_table("bulk_jobs") as b:
        for name in ("paid_at", "return_price_isk", "return_status", "return_type",
                     "return_contract_id"):
            b.drop_column(name)
    op.drop_table("bulk_plans")
