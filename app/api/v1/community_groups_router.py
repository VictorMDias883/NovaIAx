"""
API v1 niche-community endpoints (``/community/profile``, ``/community/groups``,
``/community/posts``, ``/community/comments``).

The neighbourhood reports in :mod:`app.api.v1.community_router` keep working
as their own section; blocks (``/community/blocks``) are shared by both.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_db_user
from app.models.community_group import PostKind
from app.models.user import UserRole
from app.schemas.community_group_schemas import (
    CommentCreate,
    CommentResponse,
    CommunityGroupResponse,
    CommunityProfileResponse,
    CommunityProfileUpdate,
    ContentFlagCreate,
    FlagResultResponse,
    MembershipResponse,
    PendingMemberResponse,
    PostCreate,
    PostDetailResponse,
    PostListResponse,
    PostResponse,
)
from app.services.community_group_service import CommunityGroupService
from app.utils.request import current_user_id

router = APIRouter(prefix="/community", tags=["community-groups"])


def _is_admin(current_user: dict) -> bool:
    return current_user.get("role") == UserRole.ADMIN.value


def _profile_view(profile) -> CommunityProfileResponse:  # noqa: ANN001
    return CommunityProfileResponse(
        nickname=profile.nickname, city=profile.city, state=profile.state, rules_accepted_at=profile.rules_accepted_at
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
@router.get("/profile", response_model=CommunityProfileResponse)
async def get_community_profile(
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> CommunityProfileResponse:
    """The caller's community profile; 404 until it is created."""
    return _profile_view(await CommunityGroupService(session).get_profile(current_user_id(current_user)))


@router.put("/profile", response_model=CommunityProfileResponse)
async def save_community_profile(
    payload: CommunityProfileUpdate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> CommunityProfileResponse:
    """Create or update the nickname and declared region; requires ``accept_rules``."""
    profile = await CommunityGroupService(session).save_profile(current_user_id(current_user), payload)
    return _profile_view(profile)


# ---------------------------------------------------------------------------
# Groups and memberships
# ---------------------------------------------------------------------------
@router.get("/groups", response_model=list[CommunityGroupResponse])
async def list_community_groups(
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> list[CommunityGroupResponse]:
    views = await CommunityGroupService(session).list_groups(current_user_id(current_user))
    return [CommunityGroupResponse(**view) for view in views]


@router.get("/groups/{slug}", response_model=CommunityGroupResponse)
async def get_community_group(
    slug: str,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> CommunityGroupResponse:
    return CommunityGroupResponse(**await CommunityGroupService(session).get_group(current_user_id(current_user), slug))


@router.post("/groups/{slug}/join", response_model=MembershipResponse)
async def join_community_group(
    slug: str,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> MembershipResponse:
    """Join (open/audience) or request to join (approval → ``pending``)."""
    membership = await CommunityGroupService(session).join(current_user_id(current_user), slug)
    return MembershipResponse(group_slug=slug, status=membership.status, role=membership.role)


@router.delete("/groups/{slug}/membership", status_code=204)
async def leave_community_group(
    slug: str,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await CommunityGroupService(session).leave(current_user_id(current_user), slug)


@router.get("/groups/{slug}/pending", response_model=list[PendingMemberResponse])
async def list_pending_members(
    slug: str,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> list[PendingMemberResponse]:
    """Moderators only: requests waiting for approval."""
    rows = await CommunityGroupService(session).pending_members(
        current_user_id(current_user), slug, is_admin=_is_admin(current_user)
    )
    return [PendingMemberResponse(**row) for row in rows]


@router.post("/groups/{slug}/pending/{member_id}/approve", status_code=204)
async def approve_member(
    slug: str,
    member_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await CommunityGroupService(session).decide_member(
        current_user_id(current_user), slug, member_id, approve=True, is_admin=_is_admin(current_user)
    )


@router.post("/groups/{slug}/pending/{member_id}/reject", status_code=204)
async def reject_member(
    slug: str,
    member_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await CommunityGroupService(session).decide_member(
        current_user_id(current_user), slug, member_id, approve=False, is_admin=_is_admin(current_user)
    )


# ---------------------------------------------------------------------------
# Posts
# ---------------------------------------------------------------------------
@router.get("/groups/{slug}/posts", response_model=PostListResponse)
async def list_group_posts(
    slug: str,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=50),
    kind: PostKind | None = None,
    city: str | None = Query(default=None, max_length=80),
    state: str | None = Query(default=None, max_length=2),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> PostListResponse:
    """Members only.  ``city``/``state`` narrow the feed to one region."""
    views, total = await CommunityGroupService(session).list_posts(
        current_user_id(current_user),
        slug,
        is_admin=_is_admin(current_user),
        kind=kind.value if kind else None,
        city=city.strip() if city and city.strip() else None,
        state=state.strip() if state and state.strip() else None,
        page=page,
        limit=limit,
    )
    return PostListResponse(items=[PostResponse(**view) for view in views], page=page, limit=limit, total=total)


@router.post("/groups/{slug}/posts", response_model=PostResponse, status_code=201)
async def create_group_post(
    slug: str,
    payload: PostCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> PostResponse:
    return PostResponse(**await CommunityGroupService(session).create_post(current_user_id(current_user), slug, payload))


@router.get("/posts/{post_id}", response_model=PostDetailResponse)
async def get_group_post(
    post_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> PostDetailResponse:
    view = await CommunityGroupService(session).get_post(
        current_user_id(current_user), post_id, is_admin=_is_admin(current_user)
    )
    return PostDetailResponse(**view)


@router.delete("/posts/{post_id}", status_code=204)
async def delete_group_post(
    post_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Author deletes; a moderator removes it from the group."""
    await CommunityGroupService(session).delete_post(
        current_user_id(current_user), post_id, is_admin=_is_admin(current_user)
    )


@router.post("/posts/{post_id}/flag", response_model=FlagResultResponse)
async def flag_group_post(
    post_id: int,
    payload: ContentFlagCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> FlagResultResponse:
    data = await CommunityGroupService(session).flag_post(current_user_id(current_user), post_id, payload.reason)
    return FlagResultResponse(**data)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------
@router.post("/posts/{post_id}/comments", response_model=CommentResponse, status_code=201)
async def add_post_comment(
    post_id: int,
    payload: CommentCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> CommentResponse:
    view = await CommunityGroupService(session).add_comment(current_user_id(current_user), post_id, payload)
    return CommentResponse(**view)


@router.delete("/comments/{comment_id}", status_code=204)
async def delete_post_comment(
    comment_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await CommunityGroupService(session).delete_comment(
        current_user_id(current_user), comment_id, is_admin=_is_admin(current_user)
    )


@router.post("/comments/{comment_id}/flag", response_model=FlagResultResponse)
async def flag_post_comment(
    comment_id: int,
    payload: ContentFlagCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> FlagResultResponse:
    data = await CommunityGroupService(session).flag_comment(current_user_id(current_user), comment_id, payload.reason)
    return FlagResultResponse(**data)
