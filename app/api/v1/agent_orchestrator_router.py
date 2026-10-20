"""
API v1 router for the agent tool-orchestration layer.

``POST /agents/chat`` returns a reply plus validated, audited proposals.
Backend actions run only after ``POST /agents/actions/{id}/confirm``; device
actions are returned to the app for local execution and the outcome is
reported back with ``POST /agents/actions/{id}/result``.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_router, get_redis_client, get_session, require_db_user, require_plan
from app.cache.redis_client import RedisClient
from app.clients.ai_router import AIRouter
from app.clients.plan_limits import FEATURE_AI_CHAT
from app.models.agent_action import AgentActionStatus
from app.schemas.agent_orchestration_schemas import (
    AgentActionConfirmRequest,
    AgentActionRejectRequest,
    AgentActionReportResultRequest,
    AgentActionResponse,
    AgentChatRequest,
    AgentChatResponse,
    ProposedActionResponse,
)
from app.services.agent_orchestrator_service import AgentOrchestratorService
from app.services.subscription_service import Entitlement
from app.utils.request import current_user_id

router = APIRouter(prefix="/agents", tags=["agents"])


@router.post("/chat", response_model=AgentChatResponse)
async def agent_chat(
    payload: AgentChatRequest,
    entitlement: Entitlement = Depends(require_plan(FEATURE_AI_CHAT)),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    ai_router: AIRouter = Depends(get_ai_router),
) -> AgentChatResponse:
    """Run one orchestrated turn: reply + validated, audited proposals."""
    service = AgentOrchestratorService(session, ai_router, redis_client)
    result = await service.chat(
        message=payload.message,
        user_id=current_user_id(current_user),
        role=current_user.get("role", "USER"),
        plan_tier=entitlement.plan,
    )
    return AgentChatResponse(
        reply=result["reply"],
        why=result["why"],
        sources=result["sources"],
        message_id=result["message_id"],
        safety=result["safety"],
        proposed_actions=[
            ProposedActionResponse(
                id=action.id,
                type=action.type,
                params=action.params,
                requires_confirmation=action.requires_confirmation,
                summary=action.summary,
                is_device=action.is_device,
                prepares_only=action.prepares_only,
            )
            for action in result["proposed_actions"]
        ],
    )


@router.post("/actions/{action_id}/confirm", response_model=AgentActionResponse)
async def confirm_action(
    action_id: int,
    payload: AgentActionConfirmRequest | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    ai_router: AIRouter = Depends(get_ai_router),
) -> AgentActionResponse:
    """Execute a backend action, or hand a device action to the app."""
    service = AgentOrchestratorService(session, ai_router)
    action = await service.confirm(
        user_id=current_user_id(current_user),
        role=current_user.get("role", "USER"),
        action_id=action_id,
    )
    return AgentActionResponse.model_validate(action)


@router.post("/actions/{action_id}/reject", response_model=AgentActionResponse)
async def reject_action(
    action_id: int,
    payload: AgentActionRejectRequest | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    ai_router: AIRouter = Depends(get_ai_router),
) -> AgentActionResponse:
    """Mark a proposed action as rejected by the user."""
    service = AgentOrchestratorService(session, ai_router)
    action = await service.reject(user_id=current_user_id(current_user), action_id=action_id)
    return AgentActionResponse.model_validate(action)


@router.post("/actions/{action_id}/result", response_model=AgentActionResponse)
async def report_action_result(
    payload: AgentActionReportResultRequest,
    action_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    ai_router: AIRouter = Depends(get_ai_router),
) -> AgentActionResponse:
    """Close the audit loop for a device action the app executed locally."""
    service = AgentOrchestratorService(session, ai_router)
    action = await service.report_result(
        user_id=current_user_id(current_user),
        action_id=action_id,
        status=AgentActionStatus(payload.status),
        note=payload.note,
        error_code=payload.error_code,
        detail=payload.detail,
    )
    return AgentActionResponse.model_validate(action)
