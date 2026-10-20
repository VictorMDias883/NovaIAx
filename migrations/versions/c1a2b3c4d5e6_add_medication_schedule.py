"""add daily schedule fields to medications

Extends the medication-care domain with a structured, local-time schedule:

* ``schedule_times`` — a JSON list of ``"HH:MM"`` local times (max 12) at which
  a dose is due; interpreted by the app in the user's ``/me/agent`` timezone.
* ``start_date`` — first day the schedule applies.
* ``end_date`` — last day the schedule applies (``NULL`` = open-ended).

Existing rows receive an empty schedule (``'[]'``) so the column can be added
``NOT NULL`` without a full rewrite on PostgreSQL.

Revision ID: c1a2b3c4d5e6
Revises: ab12cd34ef56
Create Date: 2026-10-14 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "ab12cd34ef56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the schedule columns to ``medications``."""
    op.add_column(
        "medications",
        sa.Column("schedule_times", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("medications", sa.Column("start_date", sa.Date(), nullable=True))
    op.add_column("medications", sa.Column("end_date", sa.Date(), nullable=True))


def downgrade() -> None:
    """Drop the schedule columns."""
    op.drop_column("medications", "end_date")
    op.drop_column("medications", "start_date")
    op.drop_column("medications", "schedule_times")
