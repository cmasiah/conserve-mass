"""Assets: assets, asset_watches, material_targets

Revision ID: 0010_assets
Revises: 0009_corp_tracking_roles
Create Date: 2026-11-28

New tables only; each skipped if it already exists.
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_assets"
down_revision = "0009_corp_tracking_roles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "assets" not in tables:
        op.create_table(
            "assets",
            sa.Column("item_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("owner_type", sa.String(12), nullable=False),
            sa.Column("owner_id", sa.BigInteger(), nullable=False),
            sa.Column("type_id", sa.BigInteger(), nullable=False),
            sa.Column("quantity", sa.BigInteger(), nullable=False),
            sa.Column("location_id", sa.BigInteger(), nullable=False),
            sa.Column("location_flag", sa.String(40), nullable=False, server_default=""),
            sa.Column("is_singleton", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("site_id", sa.BigInteger(), nullable=False),
            sa.Column("division", sa.String(12), nullable=False, server_default=""),
            sa.Column("container_id", sa.BigInteger()),
            sa.Column("container_path", sa.String(200), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        for col in ("owner_id", "type_id", "site_id", "container_id"):
            op.create_index(f"ix_assets_{col}", "assets", [col])
    if "asset_watches" not in tables:
        op.create_table(
            "asset_watches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(12), nullable=False),
            sa.Column("target_id", sa.BigInteger(), nullable=False),
            sa.Column("owner_id", sa.BigInteger()),
            sa.Column("division", sa.String(12), nullable=False, server_default=""),
            sa.Column("label", sa.String(200), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
    if "material_targets" not in tables:
        op.create_table(
            "material_targets",
            sa.Column("type_id", sa.BigInteger(), primary_key=True, autoincrement=False),
            sa.Column("min_qty", sa.BigInteger()),
            sa.Column("max_qty", sa.BigInteger()),
        )


def downgrade() -> None:
    pass
