"""Pydantic schemas for the niche-community endpoints (``/community/groups``...)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.models.community_group import GroupAccess, MembershipRole, MembershipStatus, PostKind

#: Brazilian states (UF) accepted for the declared region.
BRAZIL_STATES = frozenset(
    ["AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"]
)


def _clean_state(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    state = value.strip().upper()
    if state not in BRAZIL_STATES:
        raise ValueError("Informe a sigla de um estado brasileiro (ex.: SP).")
    return state


def _clean_city(value: str | None) -> str | None:
    if value is None:
        return None
    city = " ".join(value.split())
    return city or None


class CommunityProfileUpdate(BaseModel):
    """``PUT /community/profile`` — nickname, declared region and rules acceptance."""

    nickname: str = Field(min_length=2, max_length=40)
    city: str | None = Field(default=None, max_length=80)
    state: str | None = Field(default=None, max_length=2)
    accept_rules: bool = Field(description="Must be true: the user accepts the community rules.")

    _state = field_validator("state")(_clean_state)
    _city = field_validator("city")(_clean_city)

    @field_validator("nickname")
    @classmethod
    def _nickname(cls, value: str) -> str:
        return " ".join(value.split())


class CommunityProfileResponse(BaseModel):
    nickname: str
    city: str | None = None
    state: str | None = None
    rules_accepted_at: datetime


class CommunityGroupResponse(BaseModel):
    """A group as seen by the caller (with their membership and join eligibility)."""

    slug: str
    name: str
    description: str
    icon: str
    access: GroupAccess
    audience_slug: str | None = None
    rules: str | None = None
    member_count: int
    membership_status: MembershipStatus | None = None
    membership_role: MembershipRole | None = None
    can_join: bool
    #: pt-BR reason shown when ``can_join`` is false (e.g. activate the audience).
    join_hint: str | None = None


class MembershipResponse(BaseModel):
    group_slug: str
    status: MembershipStatus
    role: MembershipRole


class PendingMemberResponse(BaseModel):
    user_id: int
    nickname: str
    city: str | None = None
    state: str | None = None
    requested_at: datetime


class PostCreate(BaseModel):
    kind: PostKind = PostKind.TIP
    body: str = Field(min_length=3, max_length=2000)
    #: When true the post carries the author's declared city/state.
    share_region: bool = True

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("Escreva pelo menos 3 caracteres.")
        return cleaned


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=1000)

    @field_validator("body")
    @classmethod
    def _body(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("O comentário não pode ficar vazio.")
        return cleaned


class ContentFlagCreate(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class AuthorResponse(BaseModel):
    """Public identity of an author: id (for blocking) and nickname only."""

    id: int
    nickname: str


class CommentResponse(BaseModel):
    id: int
    post_id: int
    author: AuthorResponse
    body: str
    is_mine: bool
    hidden: bool
    created_at: datetime


class PostResponse(BaseModel):
    id: int
    group_slug: str
    kind: PostKind
    body: str
    city: str | None = None
    state: str | None = None
    author: AuthorResponse
    is_mine: bool
    #: True for the author when the post is hidden/removed by moderation.
    hidden: bool
    comment_count: int
    can_moderate: bool
    created_at: datetime


class PostDetailResponse(PostResponse):
    comments: list[CommentResponse]


class PostListResponse(BaseModel):
    items: list[PostResponse]
    page: int
    limit: int
    total: int


class FlagResultResponse(BaseModel):
    flagged: bool
    hidden: bool


__all__ = [
    "AuthorResponse",
    "BRAZIL_STATES",
    "CommentCreate",
    "CommentResponse",
    "CommunityGroupResponse",
    "CommunityProfileResponse",
    "CommunityProfileUpdate",
    "ContentFlagCreate",
    "FlagResultResponse",
    "MembershipResponse",
    "PendingMemberResponse",
    "PostCreate",
    "PostDetailResponse",
    "PostListResponse",
    "PostResponse",
]
