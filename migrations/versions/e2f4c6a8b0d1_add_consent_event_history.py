"""add immutable consent event history

Revision ID: e2f4c6a8b0d1
Revises: d4e5f6a7b8c9
Create Date: 2026-10-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2f4c6a8b0d1"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consent_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_consent_events_id"), "consent_events", ["id"], unique=False)
    op.create_index(op.f("ix_consent_events_user_id"), "consent_events", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_consent_events_user_id"), table_name="consent_events")
    op.drop_index(op.f("ix_consent_events_id"), table_name="consent_events")
    op.drop_table("consent_events")
