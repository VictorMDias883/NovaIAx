"""create the subscription and purchase-event tables

Adds the two tables behind Google Play billing:

* ``subscriptions`` — one row per user caching the *current* Play
  subscription (plan, normalised status, product id, SHA-256 purchase-token
  hash, expiry, auto-renew / trial flags).  The raw purchase token is never
  stored.
* ``purchase_events`` — an append-only audit trail of client verifications
  and Real Time Developer Notifications, with a sanitized JSON payload (again
  keyed by the purchase-token hash, never the token).

``subscriptions.status`` carries a ``server_default`` of ``'active'`` (same
spirit as ``roadmap_days.status``): the ORM always sets it on insert, but the
default keeps the DDL self-describing.  Boolean columns deliberately carry no
``server_default`` — the ORM supplies the value on every insert, which keeps
the DDL identical on PostgreSQL and the SQLite test database.

Revision ID: d5f3a9b1c6e2
Revises: c4e2d8f5a10b
Create Date: 2026-10-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5f3a9b1c6e2"
down_revision: str | Sequence[str] | None = "c4e2d8f5a10b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the subscription and purchase-event tables."""
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "plan",
            sa.Enum("STARTER", "PRO", "PREMIUM", name="subscription_plan"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "GRACE",
                "ON_HOLD",
                "PAUSED",
                "CANCELED",
                "EXPIRED",
                name="subscription_status",
            ),
            server_default="ACTIVE",
            nullable=False,
        ),
        sa.Column("product_id", sa.String(length=120), nullable=False),
        sa.Column("purchase_token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_renewing", sa.Boolean(), nullable=False),
        sa.Column("is_trial", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_subscriptions_id"), "subscriptions", ["id"], unique=False)
    op.create_index(op.f("ix_subscriptions_user_id"), "subscriptions", ["user_id"], unique=True)
    op.create_index(op.f("ix_subscriptions_status"), "subscriptions", ["status"], unique=False)
    op.create_index(
        op.f("ix_subscriptions_purchase_token_hash"),
        "subscriptions",
        ["purchase_token_hash"],
        unique=False,
    )

    op.create_table(
        "purchase_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("notification_type", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.String(length=120), nullable=True),
        sa.Column("purchase_token_hash", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_purchase_events_id"), "purchase_events", ["id"], unique=False)
    op.create_index(op.f("ix_purchase_events_user_id"), "purchase_events", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_purchase_events_subscription_id"),
        "purchase_events",
        ["subscription_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_purchase_events_purchase_token_hash"),
        "purchase_events",
        ["purchase_token_hash"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the subscription and purchase-event tables."""
    op.drop_index(op.f("ix_purchase_events_purchase_token_hash"), table_name="purchase_events")
    op.drop_index(op.f("ix_purchase_events_subscription_id"), table_name="purchase_events")
    op.drop_index(op.f("ix_purchase_events_user_id"), table_name="purchase_events")
    op.drop_index(op.f("ix_purchase_events_id"), table_name="purchase_events")
    op.drop_table("purchase_events")
    op.drop_index(op.f("ix_subscriptions_purchase_token_hash"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_status"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_user_id"), table_name="subscriptions")
    op.drop_index(op.f("ix_subscriptions_id"), table_name="subscriptions")
    op.drop_table("subscriptions")
    # Drop the native enum types explicitly (PostgreSQL keeps them after the
    # owning columns are gone, breaking a re-run of ``upgrade head``).
    if op.get_bind().dialect.name == "postgresql":
        for enum_name in ("subscription_status", "subscription_plan"):
            op.execute(sa.text(f'DROP TYPE IF EXISTS "{enum_name}"'))
