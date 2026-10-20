"""
API v1 router for administrator-managed system prompts.

The two read endpoints are open to any authenticated user, but the prompt
text is the agent's instruction set rather than user data: administrators
receive the full record, everyone else gets metadata only
(:class:`SystemPromptSummaryResponse`).  The mutating endpoints remain
ADMIN-only.
"""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.api.deps import get_current_user, get_session, require_admin_db_role
from app.models.user import UserRole
from app.schemas.system_prompt_schemas import (
    SystemPromptListResponse,
    SystemPromptRequest,
    SystemPromptResponse,
    SystemPromptSummaryListResponse,
    SystemPromptSummaryResponse,
)
from app.services.system_prompt_service import SystemPromptService
from app.utils.http_cache import cached_json_response

router = APIRouter(prefix="/system-prompts", tags=["system-prompts"])


def _wants_prompt_text(current_user: dict) -> bool:
    """Whether the caller is entitled to see the agent instruction text."""
    return current_user.get("role") == UserRole.ADMIN.value


@router.get("/", response_model=SystemPromptListResponse | SystemPromptSummaryListResponse)
async def list_system_prompts(
    request: Request,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response | SystemPromptListResponse | SystemPromptSummaryListResponse:
    """List the active system prompts in catalog order.

    Administrators get the full records (never cached, since they contain the
    prompt text); other authenticated users get the same envelope without the
    text, served with ``ETag``/``Cache-Control`` so it can be revalidated.
    """
    include_text = _wants_prompt_text(current_user)
    service = SystemPromptService(session)
    result = await service.list_prompts(page=page, limit=limit, include_text=include_text)
    if include_text:
        return SystemPromptListResponse(**result)
    envelope = SystemPromptSummaryListResponse(**result).model_dump(mode="json")
    return cached_json_response(request, envelope)


@router.post("/", response_model=SystemPromptResponse, status_code=201)
async def create_system_prompt(
    payload: SystemPromptRequest,
    current_user: dict = Depends(require_admin_db_role),
    session: AsyncSession = Depends(get_session),
) -> SystemPromptResponse:
    service = SystemPromptService(session)
    result = await service.create_prompt(
        tipo=payload.tipo,
        system_prompt=payload.system_prompt,
        description=payload.description,
        icon=payload.icon,
        min_plan=payload.min_plan,
        audience=payload.audience,
        active=payload.active,
        sort_order=payload.sort_order,
    )
    return SystemPromptResponse(**result)


@router.get("/{prompt_id}", response_model=SystemPromptResponse | SystemPromptSummaryResponse)
async def get_system_prompt(
    prompt_id: int = Path(..., ge=1),
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SystemPromptResponse | SystemPromptSummaryResponse:
    """Fetch one system prompt.

    The prompt text is included only for administrators; other authenticated
    users receive ``id``, ``tipo`` and ``created_at``.
    """
    include_text = _wants_prompt_text(current_user)
    service = SystemPromptService(session)
    result = await service.get_prompt(prompt_id, include_text=include_text)
    if include_text:
        return SystemPromptResponse(**result)
    return SystemPromptSummaryResponse(**result)


@router.put("/{prompt_id}", response_model=SystemPromptResponse)
async def update_system_prompt(
    payload: SystemPromptRequest,
    prompt_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_admin_db_role),
    session: AsyncSession = Depends(get_session),
) -> SystemPromptResponse:
    service = SystemPromptService(session)
    result = await service.update_prompt(
        prompt_id,
        tipo=payload.tipo,
        system_prompt=payload.system_prompt,
        description=payload.description,
        icon=payload.icon,
        min_plan=payload.min_plan,
        audience=payload.audience,
        active=payload.active,
        sort_order=payload.sort_order,
    )
    return SystemPromptResponse(**result)


@router.delete("/{prompt_id}", status_code=204)
async def delete_system_prompt(
    prompt_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_admin_db_role),
    session: AsyncSession = Depends(get_session),
) -> None:
    service = SystemPromptService(session)
    await service.delete_prompt(prompt_id)
