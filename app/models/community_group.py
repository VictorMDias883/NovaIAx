"""
SQLAlchemy ORM models for niche communities (grupos da comunidade).

A :class:`CommunityGroup` gathers people with a similar profile (students,
drivers, single mothers, ...).  Access is controlled per group:

* ``open`` — anyone with a community profile joins;
* ``audience`` — only users who activated the matching personal-agent
  audience group (``audience_slug``) in their profile;
* ``approval`` — like ``audience`` and a moderator must approve the request.

Inside a group, members publish :class:`CommunityPost` rows (tips,
experiences, questions, useful information) and :class:`CommunityComment`
replies.  Each post may carry the author's **declared** city/state so people
from the same region can find each other; the exact location is never stored.

Safety invariants (enforced by the service layer):

* only active members read or write a group's content;
* content written by someone the reader blocked is never returned;
* hidden (auto, by flags) or removed (by a moderator) content is returned only
  to its author;
* every user is shown by a nickname — never the account's full name or e-mail.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class GroupAccess(str, Enum):
    """Who may join a community group."""

    OPEN = "open"
    AUDIENCE = "audience"
    APPROVAL = "approval"


class MembershipStatus(str, Enum):
    """Lifecycle of a membership request."""

    PENDING = "pending"
    ACTIVE = "active"


class MembershipRole(str, Enum):
    """Role of a member inside one group."""

    MEMBER = "member"
    MODERATOR = "moderator"


class PostKind(str, Enum):
    """What a post shares."""

    TIP = "tip"
    EXPERIENCE = "experience"
    QUESTION = "question"
    INFO = "info"


class FlagTarget(str, Enum):
    """Kind of content a flag points at."""

    POST = "post"
    COMMENT = "comment"


class CommunityGroup(Base):
    """A niche community.  Table: ``community_groups``."""

    __tablename__ = "community_groups"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default="💬")
    access: Mapped[str] = mapped_column(String(20), nullable=False, default=GroupAccess.OPEN.value)
    #: Personal-agent audience group (``agent_groups.slug``) required to join
    #: when ``access`` is ``audience`` or ``approval``.
    audience_slug: Mapped[str | None] = mapped_column(String(80), nullable=True)
    rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CommunityProfile(Base):
    """How a user appears in the communities.  Table: ``community_profiles``."""

    __tablename__ = "community_profiles"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )
    nickname: Mapped[str] = mapped_column(String(40), nullable=False)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    rules_accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CommunityMembership(Base):
    """A user's membership in one group.  Table: ``community_memberships``."""

    __tablename__ = "community_memberships"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_community_memberships_group_user"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("community_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=MembershipRole.MEMBER.value)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=MembershipStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CommunityPost(Base):
    """A post inside a group.  Table: ``community_posts``."""

    __tablename__ = "community_posts"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("community_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default=PostKind.TIP.value)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    #: Stamped when enough members flag the post (auto-hide).
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Stamped when a moderator removes the post.
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class CommunityComment(Base):
    """A reply to a post.  Table: ``community_comments``."""

    __tablename__ = "community_comments"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("community_posts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CommunityContentFlag(Base):
    """A user reporting a post or comment.  Table: ``community_content_flags``."""

    __tablename__ = "community_content_flags"
    __table_args__ = (
        UniqueConstraint("user_id", "target_type", "target_id", name="uq_community_content_flags_user_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(10), nullable=False)
    target_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = [
    "CommunityComment",
    "CommunityContentFlag",
    "CommunityGroup",
    "CommunityMembership",
    "CommunityPost",
    "CommunityProfile",
    "FlagTarget",
    "GroupAccess",
    "MembershipRole",
    "MembershipStatus",
    "PostKind",
]
