"""add index on objectives.user_id

Every objective read filters on ``user_id``: the objectives list, the
general-agent context and the roadmap-day ownership check all start with
``WHERE user_id = ?``.  Without an index PostgreSQL has to fall back to a
sequential scan of the whole table for each of them, which gets slower as
the table grows for every user rather than for the few rows that match.

``CREATE INDEX`` (not ``CREATE UNIQUE INDEX``) is used deliberately: the
column is not unique, the index is purely for lookup speed.

Revision ID: b3d7f1a9c204
Revises: 99a87c4cfb97
Create Date: 2026-09-30 12:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d7f1a9c204"
down_revision: str | Sequence[str] | None = "99a87c4cfb97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "ix_objectives_user_id"
_TABLE_NAME = "objectives"


def upgrade() -> None:
    """Create the ``objectives.user_id`` lookup index.

    ``if_not_exists`` keeps the migration re-runnable on databases that were
    created straight from the models (``init_db``) rather than from the
    migration chain, which would otherwise fail on the duplicate name.
    """
    op.create_index(
        op.f(_INDEX_NAME),
        _TABLE_NAME,
        ["user_id"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    """Drop the ``objectives.user_id`` lookup index."""
    op.drop_index(
        op.f(_INDEX_NAME),
        table_name=_TABLE_NAME,
        if_exists=True,
    )
