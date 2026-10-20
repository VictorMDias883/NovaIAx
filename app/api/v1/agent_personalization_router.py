"""
API v1 router for the agent personalization domain.

Mounted at ``/api/v1`` (no further prefix — the task's endpoints live at
``/agent-groups``, ``/sub-agents`` and ``/me/...``).  Every endpoint requires a
real database user (API-key identities are rejected with 403).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.api.deps import get_session, require_db_user
from app.commands.agent_memory_commands import CreateUserMemoryCommand, UpdateUserMemoryCommand
from app.commands.update_agent_profile_command import UpdateAgentProfileCommand
from app.models.user_agent import UserAgent
from app.schemas.agent_personalization_schemas import (
    AgentGroupResponse,
    LearningResponse,
    LearningUpdate,
    MemoryCreate,
    MemoryResponse,
    MemoryUpdate,
    SubAgentResponse,
    UserAgentResponse,
    UserAgentUpdate,
)
from app.services.agent_personalization_service import AgentPersonalizationService
from app.services.agent_prompt_service import (
    DEFAULT_AGENT_NAME,
    DEFAULT_LANGUAGE,
    DEFAULT_TIMEZONE,
)
from app.utils.http_cache import cached_json_response
from app.utils.request import current_user_id

router = APIRouter(tags=["agent-personalization"])


@router.get("/agent-groups", response_model=list[AgentGroupResponse])
async def list_agent_groups(
    request: Request,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """List the personalization audience groups (active ones, catalog order)."""
    service = AgentPersonalizationService(session)
    groups = await service.list_groups()
    payload = [
        AgentGroupResponse(
            slug=group.slug,
            name=group.name,
            description=group.description,
            icon=group.icon,
            audience=group.audience,
            sort_order=group.sort_order,
        ).model_dump(mode="json")
        for group in groups
    ]
    return cached_json_response(request, payload)


@router.get("/sub-agents", response_model=list[SubAgentResponse])
async def list_sub_agents(
    request: Request,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """List the personalization sub-agents with their linked group slug."""
    service = AgentPersonalizationService(session)
    sub_agents = await service.list_sub_agents()
    group_slug_by_id = await service.catalog_repo.groups_by_ids(
        [sub.group_id for sub in sub_agents if sub.group_id is not None]
    )
    payload = [
        SubAgentResponse(
            slug=sub.slug,
            name=sub.name,
            description=sub.description,
            group_slug=group_slug_by_id[sub.group_id].slug if sub.group_id in group_slug_by_id else None,
            min_plan=sub.min_plan,
            icon=sub.icon,
            audience=sub.audience,
            sort_order=sub.sort_order,
        ).model_dump(mode="json")
        for sub in sub_agents
    ]
    return cached_json_response(request, payload)


def _agent_response(profile: UserAgent | None, groups: dict, sub_agents: dict) -> UserAgentResponse:
    """Render a ``UserAgentResponse`` from a (possibly missing) profile."""
    if profile is None:
        return UserAgentResponse(
            agent_name=DEFAULT_AGENT_NAME,
            tone=None,
            language=DEFAULT_LANGUAGE,
            timezone=DEFAULT_TIMEZONE,
            active_group_slugs=[],
            active_sub_agent_slugs=[],
            simplified_mode=False,
            learning_enabled=True,
            active_groups=[],
            active_sub_agents=[],
        )

    return UserAgentResponse(
        agent_name=profile.agent_name,
        tone=profile.tone,
        language=profile.language,
        timezone=profile.timezone,
        active_group_slugs=profile.active_group_slugs,
        active_sub_agent_slugs=profile.active_sub_agent_slugs,
        simplified_mode=profile.simplified_mode,
        learning_enabled=profile.learning_enabled,
        active_groups=[
            AgentGroupResponse(
                slug=group.slug,
                name=group.name,
                description=group.description,
                icon=group.icon,
                audience=group.audience,
                sort_order=group.sort_order,
            )
            for group in groups.values()
        ],
        active_sub_agents=[
            SubAgentResponse(
                slug=sub.slug,
                name=sub.name,
                description=sub.description,
                group_slug=None,
                min_plan=sub.min_plan,
                icon=sub.icon,
                audience=sub.audience,
                sort_order=sub.sort_order,
            )
            for sub in sub_agents.values()
        ],
    )


@router.get("/me/agent", response_model=UserAgentResponse)
async def get_my_agent(
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> UserAgentResponse:
    """Return the caller's personalization profile (or defaults)."""
    service = AgentPersonalizationService(session)
    profile, groups, sub_agents = await service.snapshot(current_user_id(current_user))
    return _agent_response(profile, groups, sub_agents)


@router.put("/me/agent", response_model=UserAgentResponse)
async def update_my_agent(
    payload: UserAgentUpdate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> UserAgentResponse:
    """Update the caller's personalization profile.

    422 when the agent name is offensive/impersonating or a selected slug
    does not exist (or is inactive / requires a higher plan).
    """
    command = UpdateAgentProfileCommand(**payload.model_dump(exclude_unset=True))
    service = AgentPersonalizationService(session)
    try:
        await service.update_profile(current_user_id(current_user), command)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    profile, groups, sub_agents = await service.snapshot(current_user_id(current_user))
    return _agent_response(profile, groups, sub_agents)


def _memory_response(memory) -> MemoryResponse:
    return MemoryResponse(
        key=memory.key,
        value=memory.value,
        source=memory.source,
        enabled=memory.enabled,
        created_at=memory.created_at,
    )


@router.get("/me/memories", response_model=list[MemoryResponse])
async def list_my_memories(
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> list[MemoryResponse]:
    """Return the caller's long-lived memories (most recent first)."""
    service = AgentPersonalizationService(session)
    memories = await service.list_memories(current_user_id(current_user))
    return [_memory_response(memory) for memory in memories]


@router.post("/me/memories", response_model=MemoryResponse, status_code=status.HTTP_201_CREATED)
async def create_my_memory(
    payload: MemoryCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> MemoryResponse:
    """Save a memory for the caller.

    409 when the key already exists; 422 when the key uses the reserved
    ``auto_`` prefix (owned by the auto-learning pipeline).
    """
    try:
        command = CreateUserMemoryCommand(key=payload.key, value=payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = AgentPersonalizationService(session)
    try:
        memory = await service.create_memory(current_user_id(current_user), command)
    except ValueError as exc:
        # Reserved ``auto_`` prefix; surfaced as a validation error.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if memory is None:
        raise HTTPException(status_code=409, detail="Memoria já existe para esta chave.")
    return _memory_response(memory)


@router.patch("/me/memories/{key}", response_model=MemoryResponse)
async def update_my_memory(
    key: str,
    payload: MemoryUpdate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> MemoryResponse:
    """Patch a memory's value and/or enablement; 404 when the key is unknown."""
    command = UpdateUserMemoryCommand(**payload.model_dump(exclude_unset=True))
    service = AgentPersonalizationService(session)
    memory = await service.update_memory(current_user_id(current_user), key, command)
    if memory is None:
        raise HTTPException(status_code=404, detail="Memoria não encontrada.")
    return _memory_response(memory)


@router.delete("/me/memories/{key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_memory(
    key: str,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a memory; 404 when the key is unknown."""
    service = AgentPersonalizationService(session)
    deleted = await service.delete_memory(current_user_id(current_user), key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memoria não encontrada.")


@router.put("/me/learning", response_model=LearningResponse)
async def set_my_learning(
    payload: LearningUpdate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> LearningResponse:
    """Enable/disable auto-learning of memories from the user's statements."""
    service = AgentPersonalizationService(session)
    enabled = await service.set_learning(current_user_id(current_user), payload.enabled)
    return LearningResponse(enabled=enabled)
