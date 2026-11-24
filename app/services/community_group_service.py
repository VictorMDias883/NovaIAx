"""
Service layer for niche communities.

Rules (do not weaken — they back the Play Store user-generated-content policy):

* a community profile (nickname + accepted rules) is required to join, post or
  comment; people are shown only by nickname and declared city/state;
* only **active** members (or app admins) read and write a group's content;
* ``audience``/``approval`` groups require the matching personal-agent audience
  to be active in the user's profile; ``approval`` also needs a moderator;
* content by someone the reader blocked is never returned;
* hidden (auto, after ``community_report_flag_threshold`` flags) and removed
  (by a moderator) content is returned only to its author and moderators;
* posts and comments with contact data (phone, e-mail, CPF) are refused, and
  there is a per-hour cap on posts and comments;
* banned users (``users.community_banned_at``) cannot join, post or comment.

    Router → Service → Repository → Database
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.seed_community_groups import ensure_default_groups
from app.models.community_group import (
    CommunityComment,
    CommunityGroup,
    CommunityMembership,
    CommunityPost,
    CommunityProfile,
    FlagTarget,
    GroupAccess,
    MembershipRole,
    MembershipStatus,
)
from app.repositories.community_group_repository import CommunityGroupRepository
from app.repositories.community_repository import UserBlockRepository
from app.repositories.user_agent_repository import UserAgentRepository
from app.repositories.user_repository import UserRepository
from app.utils.name_filter import blocked_name_reason

#: Per-user publishing caps (anti-spam).
MAX_POSTS_PER_HOUR = 10
MAX_COMMENTS_PER_HOUR = 60

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
#: 8+ digits, optionally separated by spaces, dots, dashes or parentheses
#: (phone numbers, CPF, CNPJ, bank accounts).
_LONG_NUMBER_RE = re.compile(r"(?:\d[\s().-]{0,2}){8,}")

_NOT_FOUND_GROUP = "Comunidade não encontrada."
_NOT_FOUND_POST = "Publicação não encontrada."
_NOT_FOUND_COMMENT = "Comentário não encontrado."


def contains_contact_data(text: str) -> bool:
    """True when ``text`` carries an e-mail or a long number (phone/CPF...)."""
    return bool(_EMAIL_RE.search(text) or _LONG_NUMBER_RE.search(text))


class CommunityGroupService:
    """Groups, memberships, posts, comments and flags for one caller."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = CommunityGroupRepository(session)
        self.blocks = UserBlockRepository(session)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _group(self, slug: str) -> CommunityGroup:
        await ensure_default_groups(self.session)
        group = await self.repo.get_group_by_slug(slug)
        if group is None or not group.active:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_GROUP)
        return group

    async def _profile_or_403(self, user_id: int) -> CommunityProfile:
        profile = await self.repo.get_profile(user_id)
        if profile is None:
            raise HTTPException(
                status_code=403, detail="Crie seu perfil da comunidade e aceite as regras antes de participar."
            )
        return profile

    async def _ensure_not_banned(self, user_id: int) -> None:
        user = await UserRepository(self.session).get_by_id(user_id)
        if user is not None and user.community_banned_at is not None:
            raise HTTPException(status_code=403, detail="Você está impedido de participar da comunidade no momento.")

    async def _active_audiences(self, user_id: int) -> set[str]:
        profile = await UserAgentRepository(self.session).get_by_user(user_id)
        return set(profile.active_group_slugs or []) if profile is not None else set()

    @staticmethod
    def _is_active(membership: CommunityMembership | None) -> bool:
        return membership is not None and membership.status == MembershipStatus.ACTIVE.value

    @staticmethod
    def _can_moderate(membership: CommunityMembership | None, *, is_admin: bool) -> bool:
        if is_admin:
            return True
        return (
            membership is not None
            and membership.status == MembershipStatus.ACTIVE.value
            and membership.role == MembershipRole.MODERATOR.value
        )

    async def _member_or_403(
        self, group: CommunityGroup, user_id: int, *, is_admin: bool
    ) -> CommunityMembership | None:
        membership = await self.repo.get_membership(group.id, user_id)
        if not is_admin and not self._is_active(membership):
            raise HTTPException(status_code=403, detail="Entre na comunidade para ver e publicar conteúdo.")
        return membership

    @staticmethod
    def _join_hint(group: CommunityGroup, audiences: set[str]) -> str | None:
        if group.access == GroupAccess.OPEN.value or not group.audience_slug:
            return None
        if group.audience_slug in audiences:
            return None
        return (
            f"Esta comunidade é para quem faz parte do público “{group.name}”. "
            "Ative-o em Agentes pessoais por público para poder entrar."
        )

    @staticmethod
    def _check_text(text: str) -> None:
        if contains_contact_data(text):
            raise HTTPException(
                status_code=422,
                detail="Por segurança, não publique telefone, e-mail ou documentos. Converse pela comunidade.",
            )

    async def _author_map(self, user_ids: list[int]) -> dict[int, str]:
        profiles = await self.repo.profiles_for(user_ids)
        return {uid: profiles[uid].nickname if uid in profiles else "Membro" for uid in user_ids}

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------
    async def get_profile(self, user_id: int) -> CommunityProfile:
        profile = await self.repo.get_profile(user_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Perfil da comunidade ainda não criado.")
        return profile

    async def save_profile(self, user_id: int, payload: Any) -> CommunityProfile:
        if not payload.accept_rules:
            raise HTTPException(status_code=422, detail="Aceite as regras da comunidade para continuar.")
        if blocked_name_reason(payload.nickname) is not None or contains_contact_data(payload.nickname):
            raise HTTPException(status_code=422, detail="Escolha outro apelido.")
        profile = await self.repo.get_profile(user_id)
        if profile is None:
            profile = CommunityProfile(user_id=user_id, rules_accepted_at=datetime.now(UTC))
        profile.nickname = payload.nickname
        profile.city = payload.city
        profile.state = payload.state
        return await self.repo.save_profile(profile)

    # ------------------------------------------------------------------
    # Groups and memberships
    # ------------------------------------------------------------------
    async def list_groups(self, user_id: int) -> list[dict]:
        await ensure_default_groups(self.session)
        groups = await self.repo.list_groups()
        memberships = await self.repo.memberships_of_user(user_id)
        counts = await self.repo.active_member_counts(group.id for group in groups)
        audiences = await self._active_audiences(user_id)
        return [self._group_view(group, memberships.get(group.id), counts.get(group.id, 0), audiences) for group in groups]

    async def get_group(self, user_id: int, slug: str) -> dict:
        group = await self._group(slug)
        membership = await self.repo.get_membership(group.id, user_id)
        counts = await self.repo.active_member_counts([group.id])
        return self._group_view(group, membership, counts.get(group.id, 0), await self._active_audiences(user_id))

    def _group_view(
        self,
        group: CommunityGroup,
        membership: CommunityMembership | None,
        member_count: int,
        audiences: set[str],
    ) -> dict:
        hint = None if membership is not None else self._join_hint(group, audiences)
        return {
            "slug": group.slug,
            "name": group.name,
            "description": group.description,
            "icon": group.icon,
            "access": group.access,
            "audience_slug": group.audience_slug,
            "rules": group.rules,
            "member_count": member_count,
            "membership_status": membership.status if membership is not None else None,
            "membership_role": membership.role if membership is not None else None,
            "can_join": membership is None and hint is None,
            "join_hint": hint,
        }

    async def join(self, user_id: int, slug: str) -> CommunityMembership:
        group = await self._group(slug)
        await self._profile_or_403(user_id)
        await self._ensure_not_banned(user_id)
        existing = await self.repo.get_membership(group.id, user_id)
        if existing is not None:
            return existing
        hint = self._join_hint(group, await self._active_audiences(user_id))
        if hint is not None:
            raise HTTPException(status_code=403, detail=hint)
        status = MembershipStatus.PENDING if group.access == GroupAccess.APPROVAL.value else MembershipStatus.ACTIVE
        return await self.repo.save_membership(
            CommunityMembership(
                group_id=group.id, user_id=user_id, role=MembershipRole.MEMBER.value, status=status.value
            )
        )

    async def leave(self, user_id: int, slug: str) -> None:
        group = await self._group(slug)
        membership = await self.repo.get_membership(group.id, user_id)
        if membership is None:
            raise HTTPException(status_code=404, detail="Você não participa desta comunidade.")
        await self.repo.delete_membership(membership)

    async def pending_members(self, user_id: int, slug: str, *, is_admin: bool) -> list[dict]:
        group = await self._group(slug)
        await self._moderator_or_403(group, user_id, is_admin=is_admin)
        pending = await self.repo.pending_memberships(group.id)
        profiles = await self.repo.profiles_for(row.user_id for row in pending)
        return [
            {
                "user_id": row.user_id,
                "nickname": profiles[row.user_id].nickname if row.user_id in profiles else "Membro",
                "city": profiles[row.user_id].city if row.user_id in profiles else None,
                "state": profiles[row.user_id].state if row.user_id in profiles else None,
                "requested_at": row.created_at,
            }
            for row in pending
        ]

    async def decide_member(
        self, user_id: int, slug: str, member_id: int, *, approve: bool, is_admin: bool
    ) -> None:
        group = await self._group(slug)
        await self._moderator_or_403(group, user_id, is_admin=is_admin)
        membership = await self.repo.get_membership(group.id, member_id)
        if membership is None or membership.user_id == user_id:
            raise HTTPException(status_code=404, detail="Pedido não encontrado.")
        if approve:
            membership.status = MembershipStatus.ACTIVE.value
            await self.repo.save_membership(membership)
        else:
            await self.repo.delete_membership(membership)

    async def _moderator_or_403(self, group: CommunityGroup, user_id: int, *, is_admin: bool) -> None:
        membership = await self.repo.get_membership(group.id, user_id)
        if not self._can_moderate(membership, is_admin=is_admin):
            raise HTTPException(status_code=403, detail="Apenas moderadores podem fazer isso.")

    # ------------------------------------------------------------------
    # Posts
    # ------------------------------------------------------------------
    async def list_posts(
        self,
        user_id: int,
        slug: str,
        *,
        is_admin: bool,
        kind: str | None,
        city: str | None,
        state: str | None,
        page: int,
        limit: int,
    ) -> tuple[list[dict], int]:
        group = await self._group(slug)
        membership = await self._member_or_403(group, user_id, is_admin=is_admin)
        blocked = await self.blocks.list_blocked_ids(user_id)
        rows, total = await self.repo.list_posts(
            group.id,
            viewer_id=user_id,
            excluded_user_ids=blocked,
            kind=kind,
            city=city,
            state=state,
            offset=(page - 1) * limit,
            limit=limit,
        )
        counts = await self.repo.comment_counts((row.id for row in rows), excluded_user_ids=blocked)
        authors = await self._author_map([row.user_id for row in rows])
        can_moderate = self._can_moderate(membership, is_admin=is_admin)
        views = [
            self._post_view(row, group, user_id, authors, counts.get(row.id, 0), can_moderate=can_moderate)
            for row in rows
        ]
        return views, total

    @staticmethod
    def _post_view(
        post: CommunityPost,
        group: CommunityGroup,
        viewer_id: int,
        authors: dict[int, str],
        comment_count: int,
        *,
        can_moderate: bool,
    ) -> dict:
        return {
            "id": post.id,
            "group_slug": group.slug,
            "kind": post.kind,
            "body": post.body,
            "city": post.city,
            "state": post.state,
            "author": {"id": post.user_id, "nickname": authors.get(post.user_id, "Membro")},
            "is_mine": post.user_id == viewer_id,
            "hidden": post.hidden_at is not None or post.removed_at is not None,
            "comment_count": comment_count,
            "can_moderate": can_moderate,
            "created_at": post.created_at,
        }

    async def create_post(self, user_id: int, slug: str, payload: Any) -> dict:
        group = await self._group(slug)
        membership = await self.repo.get_membership(group.id, user_id)
        if not self._is_active(membership):
            raise HTTPException(status_code=403, detail="Entre na comunidade para publicar.")
        profile = await self._profile_or_403(user_id)
        await self._ensure_not_banned(user_id)
        self._check_text(payload.body)
        since = datetime.now(UTC) - timedelta(hours=1)
        if await self.repo.count_posts_since(user_id, since) >= MAX_POSTS_PER_HOUR:
            raise HTTPException(status_code=429, detail="Você publicou muitas vezes seguidas. Tente de novo mais tarde.")
        post = await self.repo.save_post(
            CommunityPost(
                group_id=group.id,
                user_id=user_id,
                kind=payload.kind.value,
                body=payload.body,
                city=profile.city if payload.share_region else None,
                state=profile.state if payload.share_region else None,
            )
        )
        return self._post_view(
            post, group, user_id, {user_id: profile.nickname}, 0,
            can_moderate=self._can_moderate(membership, is_admin=False),
        )

    async def _visible_post(
        self, user_id: int, post_id: int, *, is_admin: bool
    ) -> tuple[CommunityPost, CommunityGroup, CommunityMembership | None]:
        post = await self.repo.get_post(post_id)
        if post is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_POST)
        group = await self.repo.get_group(post.group_id)
        if group is None or not group.active:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_POST)
        membership = await self.repo.get_membership(group.id, user_id)
        if not is_admin and not self._is_active(membership):
            raise HTTPException(status_code=404, detail=_NOT_FOUND_POST)
        if post.user_id != user_id:
            moderating = self._can_moderate(membership, is_admin=is_admin)
            if (post.hidden_at is not None or post.removed_at is not None) and not moderating:
                raise HTTPException(status_code=404, detail=_NOT_FOUND_POST)
            if post.user_id in await self.blocks.list_blocked_ids(user_id):
                raise HTTPException(status_code=404, detail=_NOT_FOUND_POST)
        return post, group, membership

    async def get_post(self, user_id: int, post_id: int, *, is_admin: bool) -> dict:
        post, group, membership = await self._visible_post(user_id, post_id, is_admin=is_admin)
        blocked = await self.blocks.list_blocked_ids(user_id)
        comments = await self.repo.list_comments(post.id, viewer_id=user_id, excluded_user_ids=blocked)
        authors = await self._author_map([post.user_id, *(comment.user_id for comment in comments)])
        visible_count = sum(1 for c in comments if c.hidden_at is None and c.removed_at is None)
        view = self._post_view(
            post, group, user_id, authors, visible_count,
            can_moderate=self._can_moderate(membership, is_admin=is_admin),
        )
        view["comments"] = [self._comment_view(comment, user_id, authors) for comment in comments]
        return view

    async def delete_post(self, user_id: int, post_id: int, *, is_admin: bool) -> None:
        post, _group, membership = await self._visible_post(user_id, post_id, is_admin=is_admin)
        if post.user_id == user_id:
            await self.repo.delete_post(post)
            return
        if not self._can_moderate(membership, is_admin=is_admin):
            raise HTTPException(status_code=403, detail="Você só pode apagar as suas publicações.")
        # Moderators remove (soft) so the admin panel keeps the evidence.
        post.removed_at = datetime.now(UTC)
        await self.repo.save_post(post)

    # ------------------------------------------------------------------
    # Comments
    # ------------------------------------------------------------------
    @staticmethod
    def _comment_view(comment: CommunityComment, viewer_id: int, authors: dict[int, str]) -> dict:
        return {
            "id": comment.id,
            "post_id": comment.post_id,
            "author": {"id": comment.user_id, "nickname": authors.get(comment.user_id, "Membro")},
            "body": comment.body,
            "is_mine": comment.user_id == viewer_id,
            "hidden": comment.hidden_at is not None or comment.removed_at is not None,
            "created_at": comment.created_at,
        }

    async def add_comment(self, user_id: int, post_id: int, payload: Any) -> dict:
        post, _group, membership = await self._visible_post(user_id, post_id, is_admin=False)
        if not self._is_active(membership):
            raise HTTPException(status_code=403, detail="Entre na comunidade para comentar.")
        if post.hidden_at is not None or post.removed_at is not None:
            raise HTTPException(status_code=409, detail="Esta publicação está em moderação.")
        profile = await self._profile_or_403(user_id)
        await self._ensure_not_banned(user_id)
        self._check_text(payload.body)
        since = datetime.now(UTC) - timedelta(hours=1)
        if await self.repo.count_comments_since(user_id, since) >= MAX_COMMENTS_PER_HOUR:
            raise HTTPException(status_code=429, detail="Você comentou muitas vezes seguidas. Tente de novo mais tarde.")
        comment = await self.repo.save_comment(CommunityComment(post_id=post.id, user_id=user_id, body=payload.body))
        return self._comment_view(comment, user_id, {user_id: profile.nickname})

    async def delete_comment(self, user_id: int, comment_id: int, *, is_admin: bool) -> None:
        comment = await self.repo.get_comment(comment_id)
        if comment is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_COMMENT)
        _post, _group, membership = await self._visible_post(user_id, comment.post_id, is_admin=is_admin)
        if comment.user_id == user_id:
            await self.repo.delete_comment(comment)
            return
        if not self._can_moderate(membership, is_admin=is_admin):
            raise HTTPException(status_code=403, detail="Você só pode apagar os seus comentários.")
        comment.removed_at = datetime.now(UTC)
        await self.repo.save_comment(comment)

    # ------------------------------------------------------------------
    # Flags
    # ------------------------------------------------------------------
    async def flag_post(self, user_id: int, post_id: int, reason: str | None) -> dict:
        post, _group, _membership = await self._visible_post(user_id, post_id, is_admin=False)
        if post.user_id == user_id:
            raise HTTPException(status_code=400, detail="Você não pode denunciar a própria publicação.")
        await self.repo.add_flag(user_id, FlagTarget.POST, post.id, reason)
        threshold = get_settings().community_report_flag_threshold
        if threshold > 0 and post.hidden_at is None and await self.repo.count_flags(FlagTarget.POST, post.id) >= threshold:
            post.hidden_at = datetime.now(UTC)
            await self.repo.save_post(post)
        return {"flagged": True, "hidden": post.hidden_at is not None}

    async def flag_comment(self, user_id: int, comment_id: int, reason: str | None) -> dict:
        comment = await self.repo.get_comment(comment_id)
        if comment is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_COMMENT)
        await self._visible_post(user_id, comment.post_id, is_admin=False)
        if comment.user_id == user_id:
            raise HTTPException(status_code=400, detail="Você não pode denunciar o próprio comentário.")
        if comment.user_id in await self.blocks.list_blocked_ids(user_id):
            raise HTTPException(status_code=404, detail=_NOT_FOUND_COMMENT)
        await self.repo.add_flag(user_id, FlagTarget.COMMENT, comment.id, reason)
        threshold = get_settings().community_report_flag_threshold
        flags = await self.repo.count_flags(FlagTarget.COMMENT, comment.id)
        if threshold > 0 and comment.hidden_at is None and flags >= threshold:
            comment.hidden_at = datetime.now(UTC)
            await self.repo.save_comment(comment)
        return {"flagged": True, "hidden": comment.hidden_at is not None}

    # ------------------------------------------------------------------
    # Admin panel
    # ------------------------------------------------------------------
    async def moderation_queue(self) -> dict[str, list[dict]]:
        posts, comments = await self.repo.moderation_queue()
        authors = await self._author_map([*(p.user_id for p in posts), *(c.user_id for c in comments)])
        groups = {group.id: group for group in await self.repo.list_groups(include_inactive=True)}

        async def _view(kind: FlagTarget, row: CommunityPost | CommunityComment) -> dict:
            return {
                "kind": kind.value,
                "id": row.id,
                "group": groups[row.group_id].name if isinstance(row, CommunityPost) and row.group_id in groups else "",
                "author_id": row.user_id,
                "nickname": authors.get(row.user_id, "Membro"),
                "body": row.body,
                "flags": await self.repo.count_flags(kind, row.id),
                "reasons": await self.repo.flag_reasons(kind, row.id),
                "hidden": row.hidden_at is not None,
                "removed": row.removed_at is not None,
                "created_at": row.created_at,
            }

        return {
            "posts": [await _view(FlagTarget.POST, post) for post in posts],
            "comments": [await _view(FlagTarget.COMMENT, comment) for comment in comments],
        }

    async def admin_moderate(self, kind: FlagTarget, target_id: int, *, remove: bool) -> None:
        """``remove=True`` takes the content down; ``False`` restores it and clears its flags."""
        row: CommunityPost | CommunityComment | None
        row = await self.repo.get_post(target_id) if kind is FlagTarget.POST else await self.repo.get_comment(target_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Conteúdo não encontrado.")
        if remove:
            row.removed_at = datetime.now(UTC)
        else:
            row.removed_at = None
            row.hidden_at = None
            await self.repo.clear_flags(kind, target_id)
        if isinstance(row, CommunityPost):
            await self.repo.save_post(row)
        else:
            await self.repo.save_comment(row)

    async def admin_list_groups(self) -> list[dict]:
        await ensure_default_groups(self.session)
        groups = await self.repo.list_groups(include_inactive=True)
        counts = await self.repo.active_member_counts(group.id for group in groups)
        return [
            {
                "id": group.id,
                "slug": group.slug,
                "name": group.name,
                "icon": group.icon,
                "access": group.access,
                "audience_slug": group.audience_slug,
                "active": group.active,
                "member_count": counts.get(group.id, 0),
                "pending": len(await self.repo.pending_memberships(group.id)),
            }
            for group in groups
        ]

    async def admin_create_group(
        self,
        *,
        slug: str,
        name: str,
        description: str,
        icon: str,
        access: GroupAccess,
        audience_slug: str | None,
        sort_order: int,
    ) -> CommunityGroup:
        from app.db.seed_community_groups import DEFAULT_RULES

        if access is not GroupAccess.OPEN and not audience_slug:
            raise HTTPException(status_code=422, detail="Informe o público exigido para esse tipo de acesso.")
        if await self.repo.get_group_by_slug(slug) is not None:
            raise HTTPException(status_code=409, detail="Já existe uma comunidade com esse identificador.")
        return await self.repo.create_group(
            slug=slug,
            name=name,
            description=description,
            icon=icon or "💬",
            access=access.value,
            audience_slug=audience_slug or None,
            rules=DEFAULT_RULES,
            active=True,
            sort_order=sort_order,
        )

    async def admin_set_active(self, group_id: int, *, active: bool) -> None:
        group = await self.repo.get_group(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_GROUP)
        await self.repo.update_group(group, active=active)

    async def admin_set_moderator(self, group_id: int, user_id: int, *, moderator: bool) -> None:
        """Make an existing member (or any user, joining them) a moderator, or demote."""
        group = await self.repo.get_group(group_id)
        if group is None:
            raise HTTPException(status_code=404, detail=_NOT_FOUND_GROUP)
        if await UserRepository(self.session).get_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")
        membership = await self.repo.get_membership(group.id, user_id)
        if membership is None:
            if not moderator:
                return
            membership = CommunityMembership(group_id=group.id, user_id=user_id)
        membership.status = MembershipStatus.ACTIVE.value
        membership.role = MembershipRole.MODERATOR.value if moderator else MembershipRole.MEMBER.value
        await self.repo.save_membership(membership)


__all__ = ["MAX_COMMENTS_PER_HOUR", "MAX_POSTS_PER_HOUR", "CommunityGroupService", "contains_contact_data"]
