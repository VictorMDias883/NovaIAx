"""admin console: sessions, audit log and public-schema RLS

Introduces the storage backing the operator console:

* ``users.admin_totp_secret`` — optional TOTP second factor for admins.
* ``admin_sessions`` — server-side, opaque-token panel sessions with a
  per-session CSRF token, idle clock, absolute cap and revocation marker.
* ``admin_audit_log`` — one redacted before/after row per admin mutation.

It also hardens the Supabase exposure surface: every table in the ``public``
schema gets **ROW LEVEL SECURITY enabled with no policies**.  The backend
connects as the schema/table owner, and PostgreSQL lets an owner bypass RLS
(we deliberately do **not** use ``FORCE ROW LEVEL SECURITY``, which would
also block the owner).  Anonymous PostgREST/Data-API access however matches
no policy and therefore sees nothing.  The whole RLS step is PostgreSQL-only
and is skipped on SQLite (used by the test suite).

Revision ID: d2b3c4d5e6f7
Revises: c1a2b3c4d5e6
Create Date: 2026-10-14 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2b3c4d5e6f7"
down_revision: str | Sequence[str] | None = "c1a2b3c4d5e6"
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

_DISABLE_RLS = """
DO $$
DECLARE
    tbl record;
BEGIN
    FOR tbl IN
        SELECT tablename FROM pg_tables WHERE schemaname = 'public'
    LOOP
        EXECUTE format('ALTER TABLE public.%I DISABLE ROW LEVEL SECURITY;', tbl.tablename);
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    """Create the admin-console tables and enable RLS on the public schema."""
    op.add_column("users", sa.Column("admin_totp_secret", sa.String(length=64), nullable=True))

    op.create_table(
        "admin_sessions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_token", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_agent", sa.String(length=300), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_admin_sessions_user_id", "admin_sessions", ["user_id"])
    op.create_index("ix_admin_sessions_token_hash", "admin_sessions", ["token_hash"], unique=True)
    op.create_index("ix_admin_sessions_expires_at", "admin_sessions", ["expires_at"])

    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("admin_id", sa.Integer(), nullable=True),
        sa.Column("admin_email", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.String(length=80), nullable=True),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=True),
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_admin_audit_log_admin_id", "admin_audit_log", ["admin_id"])
    op.create_index("ix_admin_audit_log_action", "admin_audit_log", ["action"])
    op.create_index("ix_admin_audit_log_entity", "admin_audit_log", ["entity"])
    op.create_index("ix_admin_audit_log_created_at", "admin_audit_log", ["created_at"])

    if op.get_bind().dialect.name == "postgresql":
        op.get_bind().exec_driver_sql(_ENABLE_RLS)


def downgrade() -> None:
    """Drop the admin-console tables and undo the public-schema RLS hardening."""
    if op.get_bind().dialect.name == "postgresql":
        op.drop_table("admin_audit_log")
        op.drop_table("admin_sessions")
        op.drop_column("users", "admin_totp_secret")
        op.get_bind().exec_driver_sql(_DISABLE_RLS)
        return

    op.drop_table("admin_audit_log")
    op.drop_table("admin_sessions")
    op.drop_column("users", "admin_totp_secret")
