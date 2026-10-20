"""promotions: promo codes, redemption ledger and informational cards

Adds the storage behind the promotions feature.  Under Google Play's payments
policy the app never changes a price or sells an entitlement outside Play
Billing, so everything here is either a **complimentary grant** or an
informational card:

* ``promo_codes`` — operator-created vouchers (grant plan / extend trial /
  bonus quota) with volume limits and a validity window.
* ``promo_redemptions`` — the durable ledger of granted entitlements (also
  used by manual admin grants, where ``code_id`` is NULL).
* ``promotions`` — informational banner cards filtered by audience.

New tables get the same "RLS enabled, no policies" hardening as the rest of
the ``public`` schema (PostgreSQL only; skipped on SQLite).

Revision ID: f1a2b3c4d5e6
Revises: e1f2a3b4c5d6
Create Date: 2026-10-19 03:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
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
    """Create the promo/promotion tables and enable RLS on the public schema."""
    op.create_table(
        "promo_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=40), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("plan", sa.String(length=20), nullable=True),
        sa.Column("duration_days", sa.Integer(), nullable=True),
        sa.Column("bonus_messages", sa.Integer(), nullable=True),
        sa.Column("max_redemptions", sa.Integer(), nullable=True),
        sa.Column("per_user_limit", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promo_codes_id", "promo_codes", ["id"])
    op.create_index("ix_promo_codes_code", "promo_codes", ["code"], unique=True)
    op.create_index("ix_promo_codes_active", "promo_codes", ["active"])

    op.create_table(
        "promo_redemptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default=sa.text("'promo'")),
        sa.Column("plan", sa.String(length=20), nullable=True),
        sa.Column("bonus_messages", sa.Integer(), nullable=True),
        sa.Column("granted_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column(
            "redeemed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["code_id"], ["promo_codes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_id", "user_id", name="uq_promo_redemptions_code_user"),
    )
    op.create_index("ix_promo_redemptions_id", "promo_redemptions", ["id"])
    op.create_index("ix_promo_redemptions_code_id", "promo_redemptions", ["code_id"])
    op.create_index("ix_promo_redemptions_user_id", "promo_redemptions", ["user_id"])

    op.create_table(
        "promotions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("emoji", sa.String(length=20), nullable=True),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("cta_label", sa.String(length=60), nullable=True),
        sa.Column("cta_target", sa.String(length=20), nullable=False, server_default=sa.text("'plans'")),
        sa.Column("cta_url", sa.String(length=500), nullable=True),
        sa.Column("audience", sa.String(length=20), nullable=False, server_default=sa.text("'all'")),
        sa.Column("audience_plans", sa.JSON(), nullable=True),
        sa.Column("audience_groups", sa.JSON(), nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_promotions_id", "promotions", ["id"])
    op.create_index("ix_promotions_active", "promotions", ["active"])

    if op.get_bind().dialect.name == "postgresql":
        op.get_bind().exec_driver_sql(_ENABLE_RLS)


def downgrade() -> None:
    """Drop the promo/promotion tables."""
    op.drop_table("promotions")
    op.drop_table("promo_redemptions")
    op.drop_table("promo_codes")
