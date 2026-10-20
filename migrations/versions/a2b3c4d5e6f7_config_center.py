"""config center: remote app config, plan-limit overrides, AI ops, contacts

Adds the storage behind the admin "Configurações" section:

* ``app_config`` — typed remote configuration the app reads at startup
  (maintenance mode, supported/recommended versions, feature flags,
  announcements).
* ``plan_limit_overrides`` — partial overrides of the in-code
  :data:`app.clients.plan_limits.PLAN_LIMITS` ceilings so an operator can
  change quotas without a redeploy.
* ``ai_provider_settings`` — runtime enable/priority per AI provider
  (credentials stay in the environment, never in the database).
* ``official_contacts.region`` / ``url`` / ``source`` — the extra fields the
  contacts CRUD manages, used by the community/emergency features.

New tables get the same "RLS enabled, no policies" hardening as the rest of
the ``public`` schema (PostgreSQL only; skipped on SQLite).

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-10-19 05:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENABLE_RLS = """
DO $$
DECLARE
    tbl record;
BEGIN
    FOR tbl IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', tbl.tablename);
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    """Create the config-center tables and extend ``official_contacts``."""
    op.create_table(
        "app_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_config_id", "app_config", ["id"])
    op.create_index("ix_app_config_key", "app_config", ["key"], unique=True)

    op.create_table(
        "plan_limit_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan", sa.String(length=20), nullable=False),
        sa.Column("daily_messages", sa.Integer(), nullable=True),
        sa.Column("daily_tokens", sa.Integer(), nullable=True),
        sa.Column("daily_device_actions", sa.Integer(), nullable=True),
        sa.Column("max_groups", sa.Integer(), nullable=True),
        sa.Column("max_sub_agents", sa.Integer(), nullable=True),
        sa.Column("models", sa.Text(), nullable=True),
        sa.Column("features", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_plan_limit_overrides_id", "plan_limit_overrides", ["id"])
    op.create_index("ix_plan_limit_overrides_plan", "plan_limit_overrides", ["plan"], unique=True)

    op.create_table(
        "ai_provider_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ai_provider_settings_id", "ai_provider_settings", ["id"])
    op.create_index(
        "ix_ai_provider_settings_name", "ai_provider_settings", ["name"], unique=True
    )

    op.add_column("official_contacts", sa.Column("region", sa.String(length=120), nullable=True))
    op.add_column("official_contacts", sa.Column("url", sa.String(length=500), nullable=True))
    op.add_column("official_contacts", sa.Column("source", sa.String(length=120), nullable=True))
    op.create_index("ix_official_contacts_region", "official_contacts", ["region"])

    if op.get_bind().dialect.name == "postgresql":
        op.get_bind().exec_driver_sql(_ENABLE_RLS)


def downgrade() -> None:
    """Drop the config-center tables and the added contact columns."""
    op.drop_index("ix_official_contacts_region", table_name="official_contacts")
    op.drop_column("official_contacts", "source")
    op.drop_column("official_contacts", "url")
    op.drop_column("official_contacts", "region")
    op.drop_table("ai_provider_settings")
    op.drop_table("plan_limit_overrides")
    op.drop_table("app_config")
