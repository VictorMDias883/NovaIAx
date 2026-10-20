"""create the agent-orchestration tables

Adds the audit trail for the agent tool-orchestration layer:

* ``agent_actions`` — every action the agent *proposed*, with its validated
  parameters and its ``proposed|confirmed|rejected|executed|failed`` lifecycle.
  Backend actions run only after the user confirms; device actions are
  executed locally by the app and reported back.
* ``ai_feedback`` — user reports (offensive/wrong/unsafe) about a specific AI
  answer, keyed by the ``message_id`` the client received.

Both tables carry an indexed ``user_id`` (owner scope).  All datetimes are
UTC.  Booleans and JSON columns carry no ``server_default`` — the ORM
supplies the value on every insert, keeping the DDL portable between
PostgreSQL and the SQLite test database.

Revision ID: f8c3d6e2a914
Revises: e7b2c4d8f1a3
Create Date: 2026-10-13 00:00:01.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8c3d6e2a914"
down_revision: str | Sequence[str] | None = "e7b2c4d8f1a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTION_TYPES = (
    "CREATE_TASK",
    "CREATE_REMINDER",
    "CREATE_NOTE",
    "ADD_CHECKLIST_ITEM",
    "LOG_EXPENSE",
    "LOG_MEDICATION_TAKEN",
    "START_STUDY_PLAN",
    "CREATE_FLASHCARDS",
    "SHOW_AGENDA",
    "MARK_DAY_COMPLETED",
    "SET_VOLUME",
    "SET_BRIGHTNESS",
    "TOGGLE_FLASHLIGHT",
    "OPEN_APP",
    "OPEN_SETTINGS_PANEL",
    "DIAL",
    "COMPOSE_MESSAGE",
    "SET_ALARM",
    "SET_TIMER",
    "CREATE_CALENDAR_EVENT",
    "NAVIGATE",
    "WEB_SEARCH",
    "PLAY_MUSIC",
    "WHAT_IS_PENDING_TODAY",
)

_ACTION_STATUSES = ("PROPOSED", "CONFIRMED", "REJECTED", "EXECUTED", "FAILED")


def upgrade() -> None:
    """Create the agent-orchestration tables."""
    op.create_table(
        "agent_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column(
            "type",
            sa.Enum(*_ACTION_TYPES, name="agent_action_type"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(*_ACTION_STATUSES, name="agent_action_status"),
            server_default="PROPOSED",
            nullable=False,
        ),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("summary", sa.String(length=300), nullable=False),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False),
        sa.Column("is_device", sa.Boolean(), nullable=False),
        sa.Column("prepares_only", sa.Boolean(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_actions_id"), "agent_actions", ["id"], unique=False)
    op.create_index(op.f("ix_agent_actions_user_id"), "agent_actions", ["user_id"], unique=False)
    op.create_index(op.f("ix_agent_actions_message_id"), "agent_actions", ["message_id"], unique=False)
    op.create_index(op.f("ix_agent_actions_type"), "agent_actions", ["type"], unique=False)
    op.create_index(op.f("ix_agent_actions_status"), "agent_actions", ["status"], unique=False)

    op.create_table(
        "ai_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_feedback_id"), "ai_feedback", ["id"], unique=False)
    op.create_index(op.f("ix_ai_feedback_user_id"), "ai_feedback", ["user_id"], unique=False)
    op.create_index(op.f("ix_ai_feedback_message_id"), "ai_feedback", ["message_id"], unique=False)


def downgrade() -> None:
    """Drop the agent-orchestration tables."""
    op.drop_index(op.f("ix_ai_feedback_message_id"), table_name="ai_feedback")
    op.drop_index(op.f("ix_ai_feedback_user_id"), table_name="ai_feedback")
    op.drop_index(op.f("ix_ai_feedback_id"), table_name="ai_feedback")
    op.drop_table("ai_feedback")

    op.drop_index(op.f("ix_agent_actions_status"), table_name="agent_actions")
    op.drop_index(op.f("ix_agent_actions_type"), table_name="agent_actions")
    op.drop_index(op.f("ix_agent_actions_message_id"), table_name="agent_actions")
    op.drop_index(op.f("ix_agent_actions_user_id"), table_name="agent_actions")
    op.drop_index(op.f("ix_agent_actions_id"), table_name="agent_actions")
    op.drop_table("agent_actions")
    # Drop the native enum types explicitly (PostgreSQL keeps them after the
    # owning columns are gone, breaking a re-run of ``upgrade head``).
    if op.get_bind().dialect.name == "postgresql":
        for enum_name in ("agent_action_status", "agent_action_type"):
            op.execute(sa.text(f'DROP TYPE IF EXISTS "{enum_name}"'))
