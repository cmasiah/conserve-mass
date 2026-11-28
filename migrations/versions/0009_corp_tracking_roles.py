"""Corporations switch + character roles

Revision ID: 0009_corp_tracking_roles
Revises: 0008_blueprint_places
Create Date: 2026-11-27

* corporations.is_home: the one corp whose contracts are polled (Conserve Mass).
* characters.link_role: 'member' (personal data only) or 'director' (also corp data).
* Before this, every linked character's corp got tracked. Keep only the home corp
  tracked and clear corp-level jobs/blueprints pulled in for the others.
Idempotent: columns are only added once, and re-running changes nothing further.
"""
import os

import sqlalchemy as sa
from alembic import op

revision = "0009_corp_tracking_roles"
down_revision = "0008_blueprint_places"
branch_labels = None
depends_on = None

CORP_SCOPES = ("esi-corporations.read_blueprints.v1", "esi-industry.read_corporation_jobs.v1")


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    corp_cols = {c["name"] for c in insp.get_columns("corporations")}
    char_cols = {c["name"] for c in insp.get_columns("characters")}
    first_run = "is_home" not in corp_cols

    if "link_role" not in char_cols:
        op.add_column("characters", sa.Column("link_role", sa.String(12), nullable=False,
                                              server_default="member"))
        like = " OR ".join(f"t.scopes LIKE '%{s}%'" for s in CORP_SCOPES)
        bind.execute(sa.text(
            "UPDATE characters SET link_role = 'director' WHERE character_id IN "
            f"(SELECT t.character_id FROM esi_tokens t WHERE {like})"))
    if not first_run:
        return
    op.add_column("corporations", sa.Column("is_home", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))

    # Pick the home corp: CORPORATION_ID if set, else the tracked corp whose contract
    # poller is an admin (the CEO's corp), else the first one tracked.
    home = None
    env = (os.environ.get("CORPORATION_ID") or "").strip()
    if env.isdigit():
        home = bind.execute(sa.text("SELECT corporation_id FROM corporations "
                                    "WHERE corporation_id = :c"), {"c": int(env)}).scalar()
    if home is None:
        home = bind.execute(sa.text(
            "SELECT c.corporation_id FROM corporations c JOIN characters ch "
            "ON ch.character_id = c.sync_character_id "
            "WHERE c.is_tracked = 1 AND ch.is_admin = 1 ORDER BY c.rowid LIMIT 1")).scalar()
    if home is None:
        home = bind.execute(sa.text("SELECT corporation_id FROM corporations "
                                    "WHERE is_tracked = 1 ORDER BY rowid LIMIT 1")).scalar()
    if home is None:
        return
    bind.execute(sa.text("UPDATE corporations SET is_home = 1 WHERE corporation_id = :h"),
                 {"h": home})
    others = [r[0] for r in bind.execute(sa.text(
        "SELECT corporation_id FROM corporations WHERE corporation_id != :h AND is_tracked = 1"),
        {"h": home})]
    for cid in others:
        bind.execute(sa.text("UPDATE corporations SET is_tracked = 0, sync_character_id = NULL "
                             "WHERE corporation_id = :c"), {"c": cid})
        for table in ("industry_jobs", "blueprints", "asset_nodes"):
            bind.execute(sa.text(f"DELETE FROM {table} WHERE owner_type = 'corporation' "
                                 "AND owner_id = :c"), {"c": cid})
        for key in (f"jobs:corp:{cid}", f"bps:corp:{cid}", f"divisions:{cid}",
                    f"assets:corp:{cid}"):
            bind.execute(sa.text("DELETE FROM sync_state WHERE key = :k"), {"k": key})


def downgrade() -> None:
    pass
