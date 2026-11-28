"""Bulk jobs: item-exchange haul contracts grouped per requester, their items, and
Jita prices for valuing them.

Revision ID: 0015_bulk_jobs
Revises: 0014_asset_bpc
Create Date: 2026-11-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_bulk_jobs"
down_revision = "0014_asset_bpc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    if "bulk_jobs" not in tables:
        op.create_table(
            "bulk_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("issuer_id", sa.BigInteger(), sa.ForeignKey("customers.character_id"),
                      nullable=False),
            sa.Column("corporation_id", sa.BigInteger(), nullable=False),
            sa.Column("title", sa.String(128)),
            sa.Column("first_issued_at", sa.DateTime(), nullable=False),
            sa.Column("last_issued_at", sa.DateTime(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="new"),
            sa.Column("dest_system_id", sa.BigInteger()),
            sa.Column("dest_override", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("agreed_reward_isk", sa.Float()),
            sa.Column("return_collateral_isk", sa.Float()),
            sa.Column("priced_at", sa.DateTime()),
            sa.Column("delivered_at", sa.DateTime()),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_bulk_jobs_issuer_id", "bulk_jobs", ["issuer_id"])
    if "contract_items" not in tables:
        op.create_table(
            "contract_items",
            sa.Column("record_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("contract_id", sa.BigInteger(),
                      sa.ForeignKey("contracts.contract_id", ondelete="CASCADE"), nullable=False),
            sa.Column("type_id", sa.Integer(), nullable=False),
            sa.Column("quantity", sa.BigInteger(), nullable=False),
            sa.Column("is_included", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_bpc", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("unit_m3", sa.Float()),
            sa.Column("unit_value", sa.Float()),
        )
        op.create_index("ix_contract_items_contract_id", "contract_items", ["contract_id"])
    if "market_prices" not in tables:
        op.create_table(
            "market_prices",
            sa.Column("type_id", sa.Integer(), primary_key=True, autoincrement=False),
            sa.Column("jita_buy", sa.Float()),
            sa.Column("jita_sell", sa.Float()),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    cols = {c["name"] for c in insp.get_columns("contracts")}
    with op.batch_alter_table("contracts") as b:
        if "price_isk" not in cols:
            b.add_column(sa.Column("price_isk", sa.Float()))
        if "job_id" not in cols:
            b.add_column(sa.Column("job_id", sa.Integer()))
            b.create_index("ix_contracts_job_id", ["job_id"])
        if "value_isk" not in cols:
            b.add_column(sa.Column("value_isk", sa.Float()))
    # Item exchanges were skipped until now: forget the cached "not modified" marker so
    # the next sync re-reads the corp's contracts and picks up existing bulk contracts.
    op.execute("UPDATE sync_state SET etag = NULL, expires_at = NULL "
               "WHERE key LIKE 'corp_contracts:%'")


def downgrade() -> None:
    with op.batch_alter_table("contracts") as b:
        b.drop_index("ix_contracts_job_id")
        b.drop_column("value_isk")
        b.drop_column("job_id")
        b.drop_column("price_isk")
    op.drop_table("market_prices")
    op.drop_table("contract_items")
    op.drop_table("bulk_jobs")
