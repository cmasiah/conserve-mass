"""industry: character skills, industry jobs, blueprints

Revision ID: 0003_industry
Revises: 0002_abuse_controls
Create Date: 2026-11-27

New tables only, so no table rebuilds (see the lesson from 0002).
"""
import sqlalchemy as sa
from alembic import op

revision = "0003_industry"
down_revision = "0002_abuse_controls"
branch_labels = None
depends_on = None

DT = sa.DateTime()


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    have = _tables()
    if "character_skills" not in have:
        op.create_table(
            "character_skills",
            sa.Column("character_id", sa.BigInteger,
                      sa.ForeignKey("characters.character_id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("skill_id", sa.BigInteger, primary_key=True, autoincrement=False),
            sa.Column("trained_level", sa.Integer, nullable=False, server_default="0"),
            sa.Column("active_level", sa.Integer, nullable=False, server_default="0"),
            sa.Column("updated_at", DT, nullable=False),
        )
    if "industry_jobs" not in have:
        op.create_table(
            "industry_jobs",
            sa.Column("job_id", sa.BigInteger, primary_key=True, autoincrement=False),
            sa.Column("owner_type", sa.String(12), nullable=False),
            sa.Column("owner_id", sa.BigInteger, nullable=False),
            sa.Column("installer_id", sa.BigInteger, nullable=False),
            sa.Column("activity_id", sa.Integer, nullable=False),
            sa.Column("blueprint_type_id", sa.BigInteger, nullable=False),
            sa.Column("product_type_id", sa.BigInteger),
            sa.Column("runs", sa.Integer, nullable=False, server_default="1"),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("location_id", sa.BigInteger),
            sa.Column("facility_id", sa.BigInteger),
            sa.Column("start_date", DT, nullable=False),
            sa.Column("end_date", DT, nullable=False),
            sa.Column("last_seen_at", DT, nullable=False),
        )
        op.create_index("ix_industry_jobs_owner_id", "industry_jobs", ["owner_id"])
        op.create_index("ix_industry_jobs_installer_id", "industry_jobs", ["installer_id"])
    if "blueprints" not in have:
        op.create_table(
            "blueprints",
            sa.Column("item_id", sa.BigInteger, primary_key=True, autoincrement=False),
            sa.Column("owner_type", sa.String(12), nullable=False),
            sa.Column("owner_id", sa.BigInteger, nullable=False),
            sa.Column("type_id", sa.BigInteger, nullable=False),
            sa.Column("location_id", sa.BigInteger, nullable=False),
            sa.Column("location_flag", sa.String(32), nullable=False),
            sa.Column("quantity", sa.Integer, nullable=False),
            sa.Column("material_efficiency", sa.Integer, nullable=False, server_default="0"),
            sa.Column("time_efficiency", sa.Integer, nullable=False, server_default="0"),
            sa.Column("runs", sa.Integer, nullable=False, server_default="-1"),
            sa.Column("last_seen_at", DT, nullable=False),
        )
        op.create_index("ix_blueprints_owner_id", "blueprints", ["owner_id"])
        op.create_index("ix_blueprints_type_id", "blueprints", ["type_id"])


def downgrade() -> None:
    op.drop_table("blueprints")
    op.drop_table("industry_jobs")
    op.drop_table("character_skills")
