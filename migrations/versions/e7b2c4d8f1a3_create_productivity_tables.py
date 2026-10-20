"""create the personal-productivity tables

Adds the owner-scoped productivity domain:

* ``tasks`` — actionable items with due date, priority, status and a simple
  recurrence (``none|daily|weekly|monthly``);
* ``reminders`` — time-anchored reminders with recurrence, status and a
  ``snoozed_until`` field;
* ``notes`` — markdown notes with JSON tags and a pin flag;
* ``checklists`` / ``checklist_items`` — category-tagged lists;
* ``habits`` / ``habit_check_ins`` — daily check-ins (unique per day) that a
  streak is computed from;
* ``focus_sessions`` — pomodoro-style start/end blocks.

Every table carries an indexed ``user_id`` (owner scope).  All datetimes are
UTC.  Booleans and JSON/text-list columns carry no ``server_default`` — the
ORM supplies the value on every insert, keeping the DDL portable between
PostgreSQL and the SQLite test database.

Revision ID: e7b2c4d8f1a3
Revises: d5f3a9b1c6e2
Create Date: 2026-10-13 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b2c4d8f1a3"
down_revision: str | Sequence[str] | None = "d5f3a9b1c6e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    """Return the two bookkeeping timestamp columns shared by most tables."""
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    """Create the personal-productivity tables."""
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "priority",
            sa.Enum("LOW", "MEDIUM", "HIGH", name="task_priority"),
            server_default="MEDIUM",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("PENDING", "DONE", "CANCELED", name="task_status"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column(
            "recurrence",
            sa.Enum("NONE", "DAILY", "WEEKLY", "MONTHLY", name="task_recurrence"),
            server_default="NONE",
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tasks_id"), "tasks", ["id"], unique=False)
    op.create_index(op.f("ix_tasks_user_id"), "tasks", ["user_id"], unique=False)
    op.create_index(op.f("ix_tasks_due_at"), "tasks", ["due_at"], unique=False)
    op.create_index(op.f("ix_tasks_status"), "tasks", ["status"], unique=False)

    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recurrence",
            sa.Enum("NONE", "DAILY", "WEEKLY", "MONTHLY", name="reminder_recurrence"),
            server_default="NONE",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("PENDING", "DONE", "CANCELED", name="reminder_status"),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_reminders_id"), "reminders", ["id"], unique=False)
    op.create_index(op.f("ix_reminders_user_id"), "reminders", ["user_id"], unique=False)
    op.create_index(op.f("ix_reminders_remind_at"), "reminders", ["remind_at"], unique=False)
    op.create_index(op.f("ix_reminders_status"), "reminders", ["status"], unique=False)

    op.create_table(
        "notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", sa.Text(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_notes_id"), "notes", ["id"], unique=False)
    op.create_index(op.f("ix_notes_user_id"), "notes", ["user_id"], unique=False)

    op.create_table(
        "checklists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "category",
            sa.Enum("COMPRAS", "CASA", "BEBE", "VIAGEM", name="checklist_category"),
            server_default="COMPRAS",
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_checklists_id"), "checklists", ["id"], unique=False)
    op.create_index(op.f("ix_checklists_user_id"), "checklists", ["user_id"], unique=False)
    op.create_index(op.f("ix_checklists_category"), "checklists", ["category"], unique=False)

    op.create_table(
        "checklist_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("checklist_id", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["checklist_id"], ["checklists.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_checklist_items_id"), "checklist_items", ["id"], unique=False)
    op.create_index(op.f("ix_checklist_items_checklist_id"), "checklist_items", ["checklist_id"], unique=False)

    op.create_table(
        "habits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_habits_id"), "habits", ["id"], unique=False)
    op.create_index(op.f("ix_habits_user_id"), "habits", ["user_id"], unique=False)

    op.create_table(
        "habit_check_ins",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("habit_id", sa.Integer(), nullable=False),
        sa.Column("check_date", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["habit_id"], ["habits.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("habit_id", "check_date", name="uq_habit_check_ins_habit_id_check_date"),
    )
    op.create_index(op.f("ix_habit_check_ins_id"), "habit_check_ins", ["id"], unique=False)
    op.create_index(op.f("ix_habit_check_ins_habit_id"), "habit_check_ins", ["habit_id"], unique=False)
    op.create_index(op.f("ix_habit_check_ins_check_date"), "habit_check_ins", ["check_date"], unique=False)

    op.create_table(
        "focus_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_focus_sessions_id"), "focus_sessions", ["id"], unique=False)
    op.create_index(op.f("ix_focus_sessions_user_id"), "focus_sessions", ["user_id"], unique=False)
    op.create_index(op.f("ix_focus_sessions_start_at"), "focus_sessions", ["start_at"], unique=False)


def downgrade() -> None:
    """Drop the personal-productivity tables."""
    op.drop_index(op.f("ix_focus_sessions_start_at"), table_name="focus_sessions")
    op.drop_index(op.f("ix_focus_sessions_user_id"), table_name="focus_sessions")
    op.drop_index(op.f("ix_focus_sessions_id"), table_name="focus_sessions")
    op.drop_table("focus_sessions")

    op.drop_index(op.f("ix_habit_check_ins_check_date"), table_name="habit_check_ins")
    op.drop_index(op.f("ix_habit_check_ins_habit_id"), table_name="habit_check_ins")
    op.drop_index(op.f("ix_habit_check_ins_id"), table_name="habit_check_ins")
    op.drop_table("habit_check_ins")

    op.drop_index(op.f("ix_habits_user_id"), table_name="habits")
    op.drop_index(op.f("ix_habits_id"), table_name="habits")
    op.drop_table("habits")

    op.drop_index(op.f("ix_checklist_items_checklist_id"), table_name="checklist_items")
    op.drop_index(op.f("ix_checklist_items_id"), table_name="checklist_items")
    op.drop_table("checklist_items")

    op.drop_index(op.f("ix_checklists_category"), table_name="checklists")
    op.drop_index(op.f("ix_checklists_user_id"), table_name="checklists")
    op.drop_index(op.f("ix_checklists_id"), table_name="checklists")
    op.drop_table("checklists")

    op.drop_index(op.f("ix_notes_user_id"), table_name="notes")
    op.drop_index(op.f("ix_notes_id"), table_name="notes")
    op.drop_table("notes")

    op.drop_index(op.f("ix_reminders_status"), table_name="reminders")
    op.drop_index(op.f("ix_reminders_remind_at"), table_name="reminders")
    op.drop_index(op.f("ix_reminders_user_id"), table_name="reminders")
    op.drop_index(op.f("ix_reminders_id"), table_name="reminders")
    op.drop_table("reminders")

    op.drop_index(op.f("ix_tasks_status"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_due_at"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_user_id"), table_name="tasks")
    op.drop_index(op.f("ix_tasks_id"), table_name="tasks")
    op.drop_table("tasks")
    # Drop the native enum types explicitly (PostgreSQL keeps them after the
    # owning columns are gone, breaking a re-run of ``upgrade head``).
    if op.get_bind().dialect.name == "postgresql":
        for enum_name in (
            "checklist_category",
            "reminder_status",
            "reminder_recurrence",
            "task_recurrence",
            "task_status",
            "task_priority",
        ):
            op.execute(sa.text(f'DROP TYPE IF EXISTS "{enum_name}"'))
