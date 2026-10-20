"""add canceled status and error_code/detail to agent_actions

Extends the device-action audit loop:

* adds ``CANCELED`` to the ``agent_action_status`` enum, reported by the app
  when the user dismisses a confirmation prompt;
* adds ``error_code`` (stable machine code, e.g. ``permission_denied``) and
  ``detail`` (optional human string) so a failed/canceled device action can be
  diagnosed without overloading the existing ``error`` column.

Revision ID: ab12cd34ef56
Revises: b0a1c2d3e4f5
Create Date: 2026-10-14 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ab12cd34ef56"
down_revision: str | Sequence[str] | None = "b0a1c2d3e4f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the CANCELED status value and the error_code/detail columns."""
    if op.get_bind().dialect.name == "postgresql":
        # Safe/idempotent: PostgreSQL 12+ supports IF NOT EXISTS.
        op.execute(sa.text("ALTER TYPE agent_action_status ADD VALUE IF NOT EXISTS 'CANCELED'"))

    op.add_column(
        "agent_actions",
        sa.Column("error_code", sa.String(length=60), nullable=True),
    )
    op.add_column(
        "agent_actions",
        sa.Column("detail", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    """Drop the error_code/detail columns.

    The ``CANCELED`` enum value is intentionally left in place: PostgreSQL does
    not support removing a value from an existing enum type.
    """
    op.drop_column("agent_actions", "detail")
    op.drop_column("agent_actions", "error_code")
