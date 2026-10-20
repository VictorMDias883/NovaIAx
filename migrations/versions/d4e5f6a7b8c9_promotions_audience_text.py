"""promotions: store audience lists as text, like every JSONList column

``f1a2b3c4d5e6`` created ``promotions.audience_plans`` and
``promotions.audience_groups`` as ``json``, but the model maps them with the
shared :class:`app.db.types.JSONList` decorator, which serialises to a text
column everywhere else in the schema (``user_agents``,
``plan_limit_overrides``, ``medications.schedule_times``, ...).  On
PostgreSQL the mismatch made every INSERT into ``promotions`` fail with
``DatatypeMismatchError`` (the ORM binds a varchar), which broke both
``scripts/seed_dev.py`` and the admin console's promotion save.

This migration converts the two columns to ``text``, casting any existing
JSON so current rows keep loading through ``JSONList``.

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-10-20 08:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Convert the audience columns from ``json`` to ``text``."""
    if op.get_bind().dialect.name != "postgresql":
        return
    for column in ("audience_plans", "audience_groups"):
        op.execute(
            sa.text(
                f"ALTER TABLE promotions ALTER COLUMN {column} TYPE TEXT "
                f"USING {column}::text"
            )
        )


def downgrade() -> None:
    """Restore the ``json`` columns (text values are valid JSON)."""
    if op.get_bind().dialect.name != "postgresql":
        return
    for column in ("audience_plans", "audience_groups"):
        op.execute(
            sa.text(
                f"ALTER TABLE promotions ALTER COLUMN {column} TYPE JSON "
                f"USING {column}::json"
            )
        )
