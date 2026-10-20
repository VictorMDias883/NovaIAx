"""create the medication, finance, study, logbook and community tables

Adds the five support domains plus the chat-message registry:

* ``medications`` / ``medication_intakes`` — medication care and adherence;
* ``expenses`` / ``incomes`` / ``bills`` / ``budgets`` — personal finance
  (integer cents);
* ``study_plans`` / ``flashcard_decks`` / ``flashcards`` / ``flashcard_reviews``
  — study plans and spaced-repetition flashcards;
* ``log_entries`` — trips / shifts / child-care log;
* ``community_reports`` / ``report_confirmations`` / ``report_flags`` /
  ``user_blocks`` / ``official_contacts`` / ``draft_complaints`` — social
  community reporting;
* ``chat_messages`` — the stable ``message_id`` registry for every chat turn.

Owner scope is an indexed ``user_id`` on every row except ``official_contacts``
(global directory).  Booleans/JSON carry an ORM-side default only.  Native
enums are dropped explicitly on downgrade so ``upgrade head`` can be re-run on
PostgreSQL.

Revision ID: b0a1c2d3e4f5
Revises: c7d1e2f3a4b5
Create Date: 2026-10-13 01:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b0a1c2d3e4f5"
down_revision: str | Sequence[str] | None = "c7d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def _created_at() -> list[sa.Column]:
    return [sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False)]


def _ix(table: str, *cols: str) -> None:
    for col in cols:
        op.create_index(op.f(f"ix_{table}_{col}"), table, [col], unique=False)


def upgrade() -> None:
    # --- Medication care -----------------------------------------------------
    op.create_table(
        "medications",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("dosage", sa.String(length=120), nullable=True),
        sa.Column("frequency", sa.String(length=120), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("medications", "id", "user_id")

    op.create_table(
        "medication_intakes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("medication_id", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Enum("PENDING", "TAKEN", "SKIPPED", name="intake_status"), server_default="TAKEN", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["medication_id"], ["medications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("medication_intakes", "id", "user_id", "medication_id", "status")

    # --- Personal finance ----------------------------------------------------
    op.create_table(
        "expenses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("expenses", "id", "user_id", "category", "occurred_at")

    op.create_table(
        "incomes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        *_created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("incomes", "id", "user_id", "occurred_at")

    op.create_table(
        "bills",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Enum("PENDING", "PAID", "OVERDUE", name="bill_status"), server_default="PENDING", nullable=False),
        sa.Column("recurrence", sa.Enum("NONE", "MONTHLY", "YEARLY", name="bill_recurrence"), server_default="NONE", nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("bills", "id", "user_id", "due_date", "status")

    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "month", "category", name="uq_budgets_user_id_month_category"),
    )
    _ix("budgets", "id", "user_id", "month")

    # --- Study ---------------------------------------------------------------
    op.create_table(
        "study_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("focus", sa.Text(), nullable=True),
        sa.Column("status", sa.Enum("ACTIVE", "COMPLETED", "ARCHIVED", name="study_plan_status"), server_default="ACTIVE", nullable=False),
        sa.Column("target_date", sa.Date(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("study_plans", "id", "user_id", "status")

    op.create_table(
        "flashcard_decks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("flashcard_decks", "id", "user_id")

    op.create_table(
        "flashcards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("deck_id", sa.Integer(), nullable=False),
        sa.Column("front", sa.Text(), nullable=False),
        sa.Column("back", sa.Text(), nullable=False),
        sa.Column("ease", sa.Float(), server_default="2.5", nullable=False),
        sa.Column("interval_days", sa.Integer(), server_default="0", nullable=False),
        sa.Column("repetitions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["deck_id"], ["flashcard_decks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("flashcards", "id", "user_id", "deck_id", "due_at")

    op.create_table(
        "flashcard_reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("flashcard_id", sa.Integer(), nullable=False),
        sa.Column("grade", sa.Integer(), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["flashcard_id"], ["flashcards.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("flashcard_reviews", "id", "user_id", "flashcard_id")

    # --- Logbook -------------------------------------------------------------
    op.create_table(
        "log_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Enum("TRIP", "SHIFT", "CHILDCARE", name="log_kind"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("log_entries", "id", "user_id", "kind", "started_at")

    # --- Community -----------------------------------------------------------
    op.create_table(
        "community_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.Enum("SECURITY", "INFRASTRUCTURE", "ENVIRONMENT", "TRANSPORT", "HEALTH", "OTHER", name="report_category"), server_default="OTHER", nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Enum("SUBMITTED", "CONFIRMED", "FLAGGED", "REMOVED", name="report_status"), server_default="SUBMITTED", nullable=False),
        sa.Column("is_anonymous", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("photo_path", sa.String(length=300), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("community_reports", "id", "user_id", "category", "status")

    op.create_table(
        "report_confirmations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        *_created_at(),
        sa.ForeignKeyConstraint(["report_id"], ["community_reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", "user_id", name="uq_report_confirmations_report_id_user_id"),
    )
    _ix("report_confirmations", "id", "report_id", "user_id")

    op.create_table(
        "report_flags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        *_created_at(),
        sa.ForeignKeyConstraint(["report_id"], ["community_reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", "user_id", name="uq_report_flags_report_id_user_id"),
    )
    _ix("report_flags", "id", "report_id", "user_id")

    op.create_table(
        "user_blocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("blocker_id", sa.Integer(), nullable=False),
        sa.Column("blocked_id", sa.Integer(), nullable=False),
        *_created_at(),
        sa.ForeignKeyConstraint(["blocker_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["blocked_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("blocker_id", "blocked_id", name="uq_user_blocks_blocker_id_blocked_id"),
    )
    _ix("user_blocks", "id", "blocker_id", "blocked_id")

    op.create_table(
        "official_contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.Enum("EMERGENCY", "POLICE", "HEALTH", "SUPPORT", "OTHER", name="official_contact_category"), server_default="OTHER", nullable=False),
        sa.Column("phone", sa.String(length=40), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(length=120), nullable=True),
        *_created_at(),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("official_contacts", "id", "category")

    op.create_table(
        "draft_complaints",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=True),
        sa.Column("recipient", sa.String(length=200), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["report_id"], ["community_reports.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("draft_complaints", "id", "user_id", "report_id")

    # --- Chat message registry ----------------------------------------------
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("agent", sa.Enum("ORCHESTRATOR", "GENERAL", "AI", "DEVICE", name="chat_agent"), nullable=False),
        *_created_at(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    _ix("chat_messages", "id", "user_id", "message_id", "agent")


def downgrade() -> None:
    for table, cols in (
        ("chat_messages", ("id", "user_id", "message_id", "agent")),
        ("draft_complaints", ("id", "user_id", "report_id")),
        ("official_contacts", ("id", "category")),
        ("user_blocks", ("id", "blocker_id", "blocked_id")),
        ("report_flags", ("id", "report_id", "user_id")),
        ("report_confirmations", ("id", "report_id", "user_id")),
        ("community_reports", ("id", "user_id", "category", "status")),
        ("log_entries", ("id", "user_id", "kind", "started_at")),
        ("flashcard_reviews", ("id", "user_id", "flashcard_id")),
        ("flashcards", ("id", "user_id", "deck_id", "due_at")),
        ("flashcard_decks", ("id", "user_id")),
        ("study_plans", ("id", "user_id", "status")),
        ("budgets", ("id", "user_id", "month")),
        ("bills", ("id", "user_id", "due_date", "status")),
        ("incomes", ("id", "user_id", "occurred_at")),
        ("expenses", ("id", "user_id", "category", "occurred_at")),
        ("medication_intakes", ("id", "user_id", "medication_id", "status")),
        ("medications", ("id", "user_id")),
    ):
        for col in cols:
            op.drop_index(op.f(f"ix_{table}_{col}"), table_name=table)
        op.drop_table(table)

    if op.get_bind().dialect.name == "postgresql":
        for enum_name in (
            "chat_agent",
            "official_contact_category",
            "report_status",
            "report_category",
            "log_kind",
            "study_plan_status",
            "bill_recurrence",
            "bill_status",
            "intake_status",
        ):
            op.execute(sa.text(f'DROP TYPE IF EXISTS "{enum_name}"'))
