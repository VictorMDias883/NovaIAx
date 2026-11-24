"""niche community groups, profiles, memberships, posts, comments and flags

Revision ID: f3a4b5c6d7e8
Revises: e2f4c6a8b0d1
Create Date: 2026-11-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "e2f4c6a8b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "community_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(length=50), nullable=False),
        sa.Column("access", sa.String(length=20), nullable=False),
        sa.Column("audience_slug", sa.String(length=80), nullable=True),
        sa.Column("rules", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_community_groups_id"), "community_groups", ["id"], unique=False)
    op.create_index(op.f("ix_community_groups_slug"), "community_groups", ["slug"], unique=True)

    op.create_table(
        "community_profiles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("nickname", sa.String(length=40), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("rules_accepted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_community_profiles_id"), "community_profiles", ["id"], unique=False)
    op.create_index(op.f("ix_community_profiles_user_id"), "community_profiles", ["user_id"], unique=True)

    op.create_table(
        "community_memberships",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["community_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("group_id", "user_id", name="uq_community_memberships_group_user"),
    )
    op.create_index(op.f("ix_community_memberships_id"), "community_memberships", ["id"], unique=False)
    op.create_index(op.f("ix_community_memberships_group_id"), "community_memberships", ["group_id"], unique=False)
    op.create_index(op.f("ix_community_memberships_user_id"), "community_memberships", ["user_id"], unique=False)

    op.create_table(
        "community_posts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("city", sa.String(length=80), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["community_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("id", "group_id", "user_id", "city", "state", "created_at"):
        op.create_index(op.f(f"ix_community_posts_{column}"), "community_posts", [column], unique=False)

    op.create_table(
        "community_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("post_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(["post_id"], ["community_posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("id", "post_id", "user_id"):
        op.create_index(op.f(f"ix_community_comments_{column}"), "community_comments", [column], unique=False)

    op.create_table(
        "community_content_flags",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=10), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "target_type", "target_id", name="uq_community_content_flags_user_target"),
    )
    for column in ("id", "user_id", "target_id"):
        op.create_index(
            op.f(f"ix_community_content_flags_{column}"), "community_content_flags", [column], unique=False
        )


def downgrade() -> None:
    op.drop_table("community_content_flags")
    op.drop_table("community_comments")
    op.drop_table("community_posts")
    op.drop_table("community_memberships")
    op.drop_table("community_profiles")
    op.drop_table("community_groups")
