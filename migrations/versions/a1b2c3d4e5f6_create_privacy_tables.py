"""create consent, deletion-request tables and users.birth_date

Adds the privacy/compliance schema:

* ``consent_records`` — one row per ``(user_id, type)`` storing the accepted
  document ``version`` and ``accepted_at``.  ``type`` is a plain string (the
  closed set lives in ``app.models.consent.CONSENT_TYPES``; keeping it a
  free column lets future consent kinds be added without a migration).
* ``deletion_requests`` — queue of account-deletion requests submitted from
  the public ``/account-deletion`` form without a password.
* ``users.birth_date`` — nullable ``DATE`` collected at registration for the
  minimum-age gate (null for pre-existing rows).

Revision ID: a1b2c3d4e5f6
Revises: f8c3d6e2a914
Create Date: 2026-10-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "f8c3d6e2a914"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the privacy tables and the ``birth_date`` column."""
    op.add_column("users", sa.Column("birth_date", sa.Date(), nullable=True))

    op.create_table(
        "consent_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("version", sa.String(length=20), nullable=False),
        sa.Column(
            "accepted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "type", name="uq_consent_records_user_id_type"),
    )
    op.create_index(op.f("ix_consent_records_id"), "consent_records", ["id"], unique=False)
    op.create_index(op.f("ix_consent_records_user_id"), "consent_records", ["user_id"], unique=False)

    op.create_table(
        "deletion_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_deletion_requests_id"), "deletion_requests", ["id"], unique=False)
    op.create_index(op.f("ix_deletion_requests_email"), "deletion_requests", ["email"], unique=False)


def downgrade() -> None:
    """Drop the privacy tables and the ``birth_date`` column."""
    op.drop_index(op.f("ix_deletion_requests_email"), table_name="deletion_requests")
    op.drop_index(op.f("ix_deletion_requests_id"), table_name="deletion_requests")
    op.drop_table("deletion_requests")
    op.drop_index(op.f("ix_consent_records_user_id"), table_name="consent_records")
    op.drop_index(op.f("ix_consent_records_id"), table_name="consent_records")
    op.drop_table("consent_records")
    op.drop_column("users", "birth_date")
