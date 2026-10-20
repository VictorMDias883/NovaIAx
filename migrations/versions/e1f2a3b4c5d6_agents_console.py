"""agents console: prompt versions + catalog metadata

Turns the agent catalog into something an operator can manage without code:

* extends ``sub_agents`` with ``active``, ``sort_order``, ``icon`` and
  ``audience`` so it can be listed/ordered/hidden like ``agent_groups``;
* extends ``system_prompts`` (the "custom-prompt agents") with ``active``,
  ``sort_order``, ``icon``, ``description``, ``min_plan`` and ``audience``;
* adds the append-only ``prompt_versions`` table so every prompt change is
  versioned, diffable and rollback-able.

Existing rows get sensible defaults (active, sort 0, neutral icon, empty
descriptions) so the columns can be added ``NOT NULL`` without a table rewrite.

Revision ID: e1f2a3b4c5d6
Revises: d2b3c4d5e6f7
Create Date: 2026-10-19 02:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add catalog metadata columns and the ``prompt_versions`` table."""
    op.alter_column(
        "system_prompts",
        "system_prompt",
        existing_type=sa.String(length=5000),
        type_=sa.Text(),
        existing_nullable=False,
    )

    op.add_column(
        "sub_agents",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "sub_agents",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "sub_agents",
        sa.Column("icon", sa.String(length=50), nullable=False, server_default=sa.text("'🧩'")),
    )
    op.add_column(
        "sub_agents",
        sa.Column("audience", sa.String(length=120), nullable=False, server_default=sa.text("''")),
    )

    op.add_column(
        "system_prompts",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column(
        "system_prompts",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "system_prompts",
        sa.Column("icon", sa.String(length=50), nullable=False, server_default=sa.text("'🤖'")),
    )
    op.add_column(
        "system_prompts",
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "system_prompts",
        sa.Column("min_plan", sa.String(length=20), nullable=False, server_default=sa.text("'free'")),
    )
    op.add_column(
        "system_prompts",
        sa.Column("audience", sa.String(length=120), nullable=False, server_default=sa.text("''")),
    )
    op.create_index("ix_system_prompts_active", "system_prompts", ["active"])

    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("entity", sa.String(length=40), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entity", "entity_id", "version", name="uq_prompt_versions_entity_version"),
    )
    op.create_index("ix_prompt_versions_id", "prompt_versions", ["id"])
    op.create_index("ix_prompt_versions_entity", "prompt_versions", ["entity"])
    op.create_index("ix_prompt_versions_entity_id", "prompt_versions", ["entity_id"])
    op.create_index("ix_prompt_versions_created_by", "prompt_versions", ["created_by"])


def downgrade() -> None:
    """Drop the prompt-versions table and the added catalog columns."""
    op.drop_index("ix_prompt_versions_created_by", table_name="prompt_versions")
    op.drop_index("ix_prompt_versions_entity_id", table_name="prompt_versions")
    op.drop_index("ix_prompt_versions_entity", table_name="prompt_versions")
    op.drop_index("ix_prompt_versions_id", table_name="prompt_versions")
    op.drop_table("prompt_versions")

    op.drop_index("ix_system_prompts_active", table_name="system_prompts")
    for column in ("audience", "min_plan", "description", "icon", "sort_order", "active"):
        op.drop_column("system_prompts", column)
    op.alter_column(
        "system_prompts",
        "system_prompt",
        existing_type=sa.Text(),
        type_=sa.String(length=5000),
        existing_nullable=False,
    )

    for column in ("audience", "icon", "sort_order", "active"):
        op.drop_column("sub_agents", column)
