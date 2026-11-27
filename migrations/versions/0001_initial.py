"""initial schema: core + couriers

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-26
"""
import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

DT = sa.DateTime()


def upgrade() -> None:
    op.create_table(
        "characters",
        sa.Column("character_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("corporation_id", sa.BigInteger),
        sa.Column("alliance_id", sa.BigInteger),
        sa.Column("is_admin", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", DT, nullable=False),
        sa.Column("updated_at", DT, nullable=False),
    )
    op.create_table(
        "esi_tokens",
        sa.Column("character_id", sa.BigInteger,
                  sa.ForeignKey("characters.character_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("refresh_token_enc", sa.LargeBinary, nullable=False),
        sa.Column("access_token_enc", sa.LargeBinary),
        sa.Column("access_expires_at", DT),
        sa.Column("scopes", sa.Text, nullable=False, server_default=""),
        sa.Column("is_valid", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_error", sa.Text),
        sa.Column("updated_at", DT, nullable=False),
    )
    op.create_table(
        "corporations",
        sa.Column("corporation_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(64)),
        sa.Column("ticker", sa.String(8)),
        sa.Column("is_tracked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("sync_character_id", sa.BigInteger,
                  sa.ForeignKey("characters.character_id", ondelete="SET NULL")),
    )
    op.create_table(
        "entity_names",
        sa.Column("entity_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("resolved_at", DT, nullable=False),
    )
    op.create_table(
        "locations",
        sa.Column("location_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(128)),
        sa.Column("kind", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("system_id", sa.BigInteger),
        sa.Column("system_name", sa.String(64)),
        sa.Column("security", sa.Float),
        sa.Column("resolved_at", DT),
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("ref", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("url", sa.String(512)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", DT, nullable=False),
        sa.Column("sent_at", DT),
        sa.UniqueConstraint("ref", "kind", name="uq_notification_ref_kind"),
    )
    op.create_table(
        "sync_state",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("etag", sa.String(256)),
        sa.Column("expires_at", DT),
        sa.Column("last_attempt_at", DT),
        sa.Column("last_success_at", DT),
        sa.Column("last_error", sa.Text),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text, nullable=False),
    )
    op.create_table(
        "customers",
        sa.Column("character_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(64)),
        sa.Column("corporation_id", sa.BigInteger),
        sa.Column("corporation_name", sa.String(64)),
        sa.Column("tag", sa.String(32)),
        sa.Column("notes", sa.Text),
        sa.Column("first_seen_at", DT, nullable=False),
    )
    op.create_table(
        "contracts",
        sa.Column("contract_id", sa.BigInteger, primary_key=True, autoincrement=False),
        sa.Column("corporation_id", sa.BigInteger,
                  sa.ForeignKey("corporations.corporation_id"), nullable=False),
        sa.Column("issuer_id", sa.BigInteger,
                  sa.ForeignKey("customers.character_id"), nullable=False),
        sa.Column("issuer_corp_id", sa.BigInteger),
        sa.Column("acceptor_id", sa.BigInteger),
        sa.Column("assignee_id", sa.BigInteger),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("title", sa.String(256)),
        sa.Column("start_location_id", sa.BigInteger, sa.ForeignKey("locations.location_id")),
        sa.Column("end_location_id", sa.BigInteger, sa.ForeignKey("locations.location_id")),
        sa.Column("volume_m3", sa.Float),
        sa.Column("reward_isk", sa.Float),
        sa.Column("collateral_isk", sa.Float),
        sa.Column("days_to_complete", sa.Integer),
        sa.Column("date_issued", DT, nullable=False),
        sa.Column("date_expired", DT),
        sa.Column("date_accepted", DT),
        sa.Column("date_completed", DT),
        sa.Column("work_status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("notes", sa.Text),
        sa.Column("first_seen_at", DT, nullable=False),
        sa.Column("last_seen_at", DT, nullable=False),
        sa.Column("raw_json", sa.Text),
    )
    op.create_index("ix_contracts_status_issued", "contracts", ["status", "date_issued"])
    op.create_index("ix_contracts_issuer", "contracts", ["issuer_id"])
    op.create_table(
        "contract_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("contract_id", sa.BigInteger,
                  sa.ForeignKey("contracts.contract_id", ondelete="CASCADE"), nullable=False),
        sa.Column("field", sa.String(32), nullable=False),
        sa.Column("old_value", sa.Text),
        sa.Column("new_value", sa.Text),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("observed_at", DT, nullable=False),
    )
    op.create_index("ix_contract_events_contract_id", "contract_events", ["contract_id"])


def downgrade() -> None:
    for t in ["contract_events", "contracts", "customers", "settings", "sync_state",
              "notifications", "locations", "entity_names", "corporations", "esi_tokens",
              "characters"]:
        op.drop_table(t)
