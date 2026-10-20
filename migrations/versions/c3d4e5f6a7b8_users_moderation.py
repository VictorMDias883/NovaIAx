"""users & moderation: suspension, last login, feedback review, report auto-hide

Backs the admin console's expanded "Usuários" and "Moderação" sections:

* ``users.suspended_at`` — set to suspend an account (403 ``account_suspended``
  on every API call, refresh tokens rejected).
* ``users.last_login_at`` — last successful interactive login, shown on the
  user detail page.
* ``users.community_banned_at`` — bars the user from posting community reports.
* ``ai_feedback.message_excerpt`` — the offending answer, but only when the
  reporter attached it; the backend never persists chat content on its own.
* ``ai_feedback.status`` — review lifecycle (pending/resolved/ignored).
* ``community_reports.hidden_at`` — set when a report is auto-hidden after
  reaching the configured flag threshold; a moderator can restore it.

Revision ID: c3d4e5f6a7b8
Revises: a2b3c4d5e6f7
Create Date: 2026-10-19 06:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the suspension, login-tracking, feedback-review and auto-hide columns."""
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users", sa.Column("community_banned_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column("ai_feedback", sa.Column("message_excerpt", sa.Text(), nullable=True))
    op.add_column(
        "ai_feedback",
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_index("ix_ai_feedback_status", "ai_feedback", ["status"])

    op.add_column(
        "community_reports", sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_community_reports_hidden_at", "community_reports", ["hidden_at"])


def downgrade() -> None:
    """Drop the suspension, login-tracking, feedback-review and auto-hide columns."""
    op.drop_index("ix_community_reports_hidden_at", table_name="community_reports")
    op.drop_column("community_reports", "hidden_at")

    op.drop_index("ix_ai_feedback_status", table_name="ai_feedback")
    op.drop_column("ai_feedback", "status")
    op.drop_column("ai_feedback", "message_excerpt")

    op.drop_column("users", "community_banned_at")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "suspended_at")
