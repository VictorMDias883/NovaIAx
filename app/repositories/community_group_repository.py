"""
Repository layer for niche communities (groups, profiles, memberships, posts,
comments and content flags).  Pure data access: every visibility and
permission rule lives in :mod:`app.services.community_group_service`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.community_group import (
    CommunityComment,
    CommunityContentFlag,
    CommunityGroup,
    CommunityMembership,
    CommunityPost,
    CommunityProfile,
    FlagTarget,
    MembershipStatus,
)


class CommunityGroupRepository:
    """Data access for the community-groups domain."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _save(self, obj: Any) -> Any:
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj

    async def commit(self) -> None:
        await self.session.commit()

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------
    async def list_groups(self, *, include_inactive: bool = False) -> list[CommunityGroup]:
        stmt = select(CommunityGroup).order_by(CommunityGroup.sort_order, CommunityGroup.id)
        if not include_inactive:
            stmt = stmt.where(CommunityGroup.active.is_(True))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_group_by_slug(self, slug: str) -> CommunityGroup | None:
        stmt = select(CommunityGroup).where(CommunityGroup.slug == slug)
        return (await self.session.execute(stmt)).scalars().first()

    async def get_group(self, group_id: int) -> CommunityGroup | None:
        return await self.session.get(CommunityGroup, group_id)

    async def create_group(self, **fields: Any) -> CommunityGroup:
        return await self._save(CommunityGroup(**fields))

    async def update_group(self, group: CommunityGroup, **fields: Any) -> CommunityGroup:
        for key, value in fields.items():
            setattr(group, key, value)
        return await self._save(group)

    async def active_member_counts(self, group_ids: Iterable[int]) -> dict[int, int]:
        ids = list(group_ids)
        if not ids:
            return {}
        stmt = (
            select(CommunityMembership.group_id, func.count())
            .where(
                CommunityMembership.group_id.in_(ids),
                CommunityMembership.status == MembershipStatus.ACTIVE.value,
            )
            .group_by(CommunityMembership.group_id)
        )
        return {int(group_id): int(count) for group_id, count in (await self.session.execute(stmt)).all()}

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------
    async def get_profile(self, user_id: int) -> CommunityProfile | None:
        stmt = select(CommunityProfile).where(CommunityProfile.user_id == user_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def profiles_for(self, user_ids: Iterable[int]) -> dict[int, CommunityProfile]:
        ids = list(set(user_ids))
        if not ids:
            return {}
        stmt = select(CommunityProfile).where(CommunityProfile.user_id.in_(ids))
        return {row.user_id: row for row in (await self.session.execute(stmt)).scalars().all()}

    async def save_profile(self, profile: CommunityProfile) -> CommunityProfile:
        return await self._save(profile)

    # ------------------------------------------------------------------
    # Memberships
    # ------------------------------------------------------------------
    async def get_membership(self, group_id: int, user_id: int) -> CommunityMembership | None:
        stmt = select(CommunityMembership).where(
            CommunityMembership.group_id == group_id, CommunityMembership.user_id == user_id
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def memberships_of_user(self, user_id: int) -> dict[int, CommunityMembership]:
        stmt = select(CommunityMembership).where(CommunityMembership.user_id == user_id)
        return {row.group_id: row for row in (await self.session.execute(stmt)).scalars().all()}

    async def save_membership(self, membership: CommunityMembership) -> CommunityMembership:
        return await self._save(membership)

    async def delete_membership(self, membership: CommunityMembership) -> None:
        await self.session.delete(membership)
        await self.session.commit()

    async def pending_memberships(self, group_id: int) -> list[CommunityMembership]:
        stmt = (
            select(CommunityMembership)
            .where(
                CommunityMembership.group_id == group_id,
                CommunityMembership.status == MembershipStatus.PENDING.value,
            )
            .order_by(CommunityMembership.created_at, CommunityMembership.id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------
    # Posts
    # ------------------------------------------------------------------
    async def get_post(self, post_id: int) -> CommunityPost | None:
        return await self.session.get(CommunityPost, post_id)

    async def save_post(self, post: CommunityPost) -> CommunityPost:
        return await self._save(post)

    async def delete_post(self, post: CommunityPost) -> None:
        comment_ids = select(CommunityComment.id).where(CommunityComment.post_id == post.id)
        await self.session.execute(
            delete(CommunityContentFlag).where(
                CommunityContentFlag.target_type == FlagTarget.COMMENT.value,
                CommunityContentFlag.target_id.in_(comment_ids),
            )
        )
        await self.session.execute(
            delete(CommunityContentFlag).where(
                CommunityContentFlag.target_type == FlagTarget.POST.value,
                CommunityContentFlag.target_id == post.id,
            )
        )
        await self.session.execute(delete(CommunityComment).where(CommunityComment.post_id == post.id))
        await self.session.delete(post)
        await self.session.commit()

    async def list_posts(
        self,
        group_id: int,
        *,
        viewer_id: int,
        excluded_user_ids: set[int],
        kind: str | None,
        city: str | None,
        state: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[CommunityPost], int]:
        """Visible posts of a group: other people's hidden/removed posts are skipped."""
        visible_to_all = CommunityPost.hidden_at.is_(None) & CommunityPost.removed_at.is_(None)
        clauses: list[Any] = [
            CommunityPost.group_id == group_id,
            visible_to_all | (CommunityPost.user_id == viewer_id),
        ]
        if excluded_user_ids:
            clauses.append(CommunityPost.user_id.not_in(excluded_user_ids))
        if kind:
            clauses.append(CommunityPost.kind == kind)
        if city:
            clauses.append(func.lower(CommunityPost.city) == city.lower())
        if state:
            clauses.append(CommunityPost.state == state.upper())
        total = int((await self.session.execute(select(func.count()).select_from(CommunityPost).where(*clauses))).scalar_one())
        stmt = (
            select(CommunityPost)
            .where(*clauses)
            .order_by(CommunityPost.created_at.desc(), CommunityPost.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all()), total

    async def count_posts_since(self, user_id: int, since: datetime) -> int:
        stmt = select(func.count()).select_from(CommunityPost).where(
            CommunityPost.user_id == user_id, CommunityPost.created_at >= since
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def comment_counts(self, post_ids: Iterable[int], *, excluded_user_ids: set[int]) -> dict[int, int]:
        ids = list(post_ids)
        if not ids:
            return {}
        clauses: list[Any] = [
            CommunityComment.post_id.in_(ids),
            CommunityComment.hidden_at.is_(None),
            CommunityComment.removed_at.is_(None),
        ]
        if excluded_user_ids:
            clauses.append(CommunityComment.user_id.not_in(excluded_user_ids))
        stmt = select(CommunityComment.post_id, func.count()).where(*clauses).group_by(CommunityComment.post_id)
        return {int(post_id): int(count) for post_id, count in (await self.session.execute(stmt)).all()}

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------
    async def get_comment(self, comment_id: int) -> CommunityComment | None:
        return await self.session.get(CommunityComment, comment_id)

    async def save_comment(self, comment: CommunityComment) -> CommunityComment:
        return await self._save(comment)

    async def delete_comment(self, comment: CommunityComment) -> None:
        await self.session.execute(
            delete(CommunityContentFlag).where(
                CommunityContentFlag.target_type == FlagTarget.COMMENT.value,
                CommunityContentFlag.target_id == comment.id,
            )
        )
        await self.session.delete(comment)
        await self.session.commit()

    async def list_comments(
        self, post_id: int, *, viewer_id: int, excluded_user_ids: set[int]
    ) -> list[CommunityComment]:
        visible_to_all = CommunityComment.hidden_at.is_(None) & CommunityComment.removed_at.is_(None)
        clauses: list[Any] = [
            CommunityComment.post_id == post_id,
            visible_to_all | (CommunityComment.user_id == viewer_id),
        ]
        if excluded_user_ids:
            clauses.append(CommunityComment.user_id.not_in(excluded_user_ids))
        stmt = select(CommunityComment).where(*clauses).order_by(CommunityComment.created_at, CommunityComment.id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_comments_since(self, user_id: int, since: datetime) -> int:
        stmt = select(func.count()).select_from(CommunityComment).where(
            CommunityComment.user_id == user_id, CommunityComment.created_at >= since
        )
        return int((await self.session.execute(stmt)).scalar_one())

    # ------------------------------------------------------------------
    # Flags
    # ------------------------------------------------------------------
    async def add_flag(self, user_id: int, target: FlagTarget, target_id: int, reason: str | None) -> bool:
        """Record a flag; ``False`` when this user already flagged the target."""
        stmt = select(CommunityContentFlag.id).where(
            CommunityContentFlag.user_id == user_id,
            CommunityContentFlag.target_type == target.value,
            CommunityContentFlag.target_id == target_id,
        )
        if (await self.session.execute(stmt)).first() is not None:
            return False
        await self._save(
            CommunityContentFlag(user_id=user_id, target_type=target.value, target_id=target_id, reason=reason)
        )
        return True

    async def count_flags(self, target: FlagTarget, target_id: int) -> int:
        stmt = select(func.count()).select_from(CommunityContentFlag).where(
            CommunityContentFlag.target_type == target.value, CommunityContentFlag.target_id == target_id
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def flag_reasons(self, target: FlagTarget, target_id: int) -> list[str]:
        stmt = select(CommunityContentFlag.reason).where(
            CommunityContentFlag.target_type == target.value,
            CommunityContentFlag.target_id == target_id,
            CommunityContentFlag.reason.is_not(None),
        )
        return [str(reason) for reason in (await self.session.execute(stmt)).scalars().all()]

    async def clear_flags(self, target: FlagTarget, target_id: int) -> None:
        await self.session.execute(
            delete(CommunityContentFlag).where(
                CommunityContentFlag.target_type == target.value, CommunityContentFlag.target_id == target_id
            )
        )
        await self.session.commit()

    async def moderation_queue(self, *, limit: int = 100) -> tuple[list[CommunityPost], list[CommunityComment]]:
        """Flagged, hidden or removed content, newest first (admin panel)."""
        flagged_posts = select(CommunityContentFlag.target_id).where(
            CommunityContentFlag.target_type == FlagTarget.POST.value
        )
        flagged_comments = select(CommunityContentFlag.target_id).where(
            CommunityContentFlag.target_type == FlagTarget.COMMENT.value
        )
        posts = (
            await self.session.execute(
                select(CommunityPost)
                .where(
                    CommunityPost.id.in_(flagged_posts)
                    | CommunityPost.hidden_at.is_not(None)
                    | CommunityPost.removed_at.is_not(None)
                )
                .order_by(CommunityPost.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        comments = (
            await self.session.execute(
                select(CommunityComment)
                .where(
                    CommunityComment.id.in_(flagged_comments)
                    | CommunityComment.hidden_at.is_not(None)
                    | CommunityComment.removed_at.is_not(None)
                )
                .order_by(CommunityComment.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        return list(posts), list(comments)


__all__ = ["CommunityGroupRepository"]
