"""add ON DELETE rules to user-owned foreign keys

Every row that belongs to a user should disappear with that user.  The
original schema created these foreign keys without an ``ondelete`` rule, so
deleting a user either failed on the RESTRICT default or had to be driven by
hand-written deletes in application code.  This migration makes the database
the source of truth:

* ``objectives.user_id``, ``roadmap_days.objective_id``,
  ``consent_records.user_id``, ``subscriptions.user_id``,
  ``user_agents.user_id``, ``user_memories.user_id`` and
  ``sub_agents.group_id`` → ``CASCADE``.
* ``purchase_events.user_id`` → ``SET NULL`` (the audit row is kept for
  accounting even when the owning user is gone; it is nullable for exactly
  this reason).

PostgreSQL only: SQLite cannot alter a constraint in place, and the test
suite builds its schema from the model metadata (which already carries the
rules), so there is nothing to do there.

Revision ID: c7d1e2f3a4b5
Revises: a1b2c3d4e5f6
Create Date: 2026-10-13 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d1e2f3a4b5"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``(constraint, table, column, referenced table, ondelete)`` for each FK we
#: rewrite.  Constraint names are the PostgreSQL defaults produced when the
#: tables were created; they are also the names SQLAlchemy auto-generates.
_FOREIGN_KEYS: list[tuple[str, str, str, str, str]] = [
    ("objectives_user_id_fkey", "objectives", "user_id", "users", "CASCADE"),
    ("roadmap_days_objective_id_fkey", "roadmap_days", "objective_id", "objectives", "CASCADE"),
    ("consent_records_user_id_fkey", "consent_records", "user_id", "users", "CASCADE"),
    ("subscriptions_user_id_fkey", "subscriptions", "user_id", "users", "CASCADE"),
    ("user_agents_user_id_fkey", "user_agents", "user_id", "users", "CASCADE"),
    ("user_memories_user_id_fkey", "user_memories", "user_id", "users", "CASCADE"),
    ("sub_agents_group_id_fkey", "sub_agents", "group_id", "agent_groups", "CASCADE"),
    ("purchase_events_user_id_fkey", "purchase_events", "user_id", "users", "SET NULL"),
]


def _is_postgresql() -> bool:
    """True when the migration is running on PostgreSQL."""
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Recreate the user-owned foreign keys with their delete rules."""
    if not _is_postgresql():
        return
    for constraint, table, column, referenced, ondelete in _FOREIGN_KEYS:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, referenced, [column], ["id"], ondelete=ondelete)


def downgrade() -> None:
    """Restore the original rule-less foreign keys."""
    if not _is_postgresql():
        return
    for constraint, table, column, referenced, _ in _FOREIGN_KEYS:
        op.drop_constraint(constraint, table, type_="foreignkey")
        op.create_foreign_key(constraint, table, referenced, [column], ["id"])
