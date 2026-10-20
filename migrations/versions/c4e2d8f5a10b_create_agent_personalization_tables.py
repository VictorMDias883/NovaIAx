"""create the agent personalization tables

Adds the four tables behind the agent personalization domain:

* ``agent_groups`` — the selectable audience catalog (single mothers,
  elderly, students, truck drivers, ...) with PT-BR descriptions and a
  tailored system-prompt fragment per group;
* ``sub_agents`` — focused capability modules (finances, health, ...) that
  can be activated on top of the audience groups, each gated by ``min_plan``;
* ``user_agents`` — one personalization profile per user (agent name, tone,
  language/timezone, active slugs, simplified/learning toggles);
* ``user_memories`` — per-user long-lived facts, either explicitly saved or
  auto-learned from explicit statements (``source user|learned``), unique by
  ``(user_id, key)``.

Booleans deliberately carry no ``server_default``: the ORM supplies the value
on every insert, which keeps the DDL identical on PostgreSQL and the SQLite
test database (where ``'true'``/``'1'`` defaults are not portable).

Revision ID: c4e2d8f5a10b
Revises: b3d7f1a9c204
Create Date: 2026-10-10 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4e2d8f5a10b"
down_revision: str | Sequence[str] | None = "b3d7f1a9c204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the agent personalization tables."""
    op.create_table(
        "agent_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(length=50), nullable=False),
        sa.Column("audience", sa.String(length=120), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_groups_id"), "agent_groups", ["id"], unique=False)
    op.create_index(op.f("ix_agent_groups_slug"), "agent_groups", ["slug"], unique=True)

    op.create_table(
        "sub_agents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("min_plan", sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["agent_groups.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sub_agents_group_id"), "sub_agents", ["group_id"], unique=False)
    op.create_index(op.f("ix_sub_agents_id"), "sub_agents", ["id"], unique=False)
    op.create_index(op.f("ix_sub_agents_slug"), "sub_agents", ["slug"], unique=True)

    op.create_table(
        "user_agents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("agent_name", sa.String(length=30), nullable=False),
        sa.Column("tone", sa.String(length=80), nullable=True),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("timezone", sa.String(length=50), nullable=False),
        sa.Column("active_group_slugs", sa.Text(), nullable=False),
        sa.Column("active_sub_agent_slugs", sa.Text(), nullable=False),
        sa.Column("simplified_mode", sa.Boolean(), nullable=False),
        sa.Column("learning_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_agents_id"), "user_agents", ["id"], unique=False)
    op.create_index(op.f("ix_user_agents_user_id"), "user_agents", ["user_id"], unique=True)

    op.create_table(
        "user_memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("value", sa.String(length=500), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_user_memories_user_id_key"),
    )
    op.create_index(op.f("ix_user_memories_id"), "user_memories", ["id"], unique=False)
    op.create_index(op.f("ix_user_memories_user_id"), "user_memories", ["user_id"], unique=False)


def downgrade() -> None:
    """Drop the agent personalization tables."""
    op.drop_index(op.f("ix_user_memories_user_id"), table_name="user_memories")
    op.drop_index(op.f("ix_user_memories_id"), table_name="user_memories")
    op.drop_table("user_memories")
    op.drop_index(op.f("ix_user_agents_user_id"), table_name="user_agents")
    op.drop_index(op.f("ix_user_agents_id"), table_name="user_agents")
    op.drop_table("user_agents")
    op.drop_index(op.f("ix_sub_agents_slug"), table_name="sub_agents")
    op.drop_index(op.f("ix_sub_agents_id"), table_name="sub_agents")
    op.drop_index(op.f("ix_sub_agents_group_id"), table_name="sub_agents")
    op.drop_table("sub_agents")
    op.drop_index(op.f("ix_agent_groups_slug"), table_name="agent_groups")
    op.drop_index(op.f("ix_agent_groups_id"), table_name="agent_groups")
    op.drop_table("agent_groups")
