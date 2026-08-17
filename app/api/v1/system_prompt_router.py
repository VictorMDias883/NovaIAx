"""
API v1 router for administrator-managed system prompts.
"""

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session, require_admin
from app.schemas.system_prompt_schemas import (
    SystemPromptListResponse,
    SystemPromptRequest,
    SystemPromptResponse,
)
from app.services.system_prompt_service import SystemPromptService

router = APIRouter(prefix="/system-prompts", tags=["system-prompts"])


@router.get("/", response_model=SystemPromptListResponse)
async def list_system_prompts(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SystemPromptListResponse:
    service = SystemPromptService(session)
    result = await service.list_prompts(page=page, limit=limit)
    return SystemPromptListResponse(**result)


@router.post("/", response_model=SystemPromptResponse, status_code=201)
async def create_system_prompt(
    payload: SystemPromptRequest,
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SystemPromptResponse:
    service = SystemPromptService(session)
    result = await service.create_prompt(tipo=payload.tipo, system_prompt=payload.system_prompt)
    return SystemPromptResponse(**result)


@router.get("/{prompt_id}", response_model=SystemPromptResponse)
async def get_system_prompt(
    prompt_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SystemPromptResponse:
    service = SystemPromptService(session)
    result = await service.get_prompt(prompt_id)
    return SystemPromptResponse(**result)


@router.put("/{prompt_id}", response_model=SystemPromptResponse)
async def update_system_prompt(
    payload: SystemPromptRequest,
    prompt_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> SystemPromptResponse:
    service = SystemPromptService(session)
    result = await service.update_prompt(prompt_id, tipo=payload.tipo, system_prompt=payload.system_prompt)
    return SystemPromptResponse(**result)


@router.delete("/{prompt_id}", status_code=204)
async def delete_system_prompt(
    prompt_id: int = Path(..., ge=1),
    current_user: dict = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    service = SystemPromptService(session)
    await service.delete_prompt(prompt_id)
