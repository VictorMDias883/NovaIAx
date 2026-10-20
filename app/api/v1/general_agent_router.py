"""
API v1 router for the general agent.

The general agent is a conversational assistant that can consult the
authenticated user's objectives and report which days of the roadmap
were fulfilled.  It is now a thin wrapper over the shared orchestrator in
conversational-only mode (no tool proposals).
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_router, get_entitlement, get_redis_client, get_session, require_db_user
from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient
from app.clients.ai_router import AIRouter
from app.schemas.general_agent_schemas import (
    GeneralAgentConversationClearResponse,
    GeneralAgentRequest,
    GeneralAgentResponse,
)
from app.services.agent_orchestrator_service import AgentOrchestratorService
from app.services.general_agent_service import GeneralAgentService
from app.services.subscription_service import Entitlement
from app.utils.request import current_user_id

router = APIRouter(prefix="/agents/general", tags=["agents"])


@router.post("/chat", response_model=GeneralAgentResponse)
async def general_agent_chat(
    payload: GeneralAgentRequest,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    ai_router: AIRouter = Depends(get_ai_router),
    entitlement: Entitlement = Depends(get_entitlement),
) -> GeneralAgentResponse:
    """Run the general-agent flow: chat with access to the user's objectives.

    Delegates to :class:`AgentOrchestratorService` in conversational-only
    mode, so behaviour (personalization, context, cached history) is
    unchanged while the implementation is shared with the tool layer.
    """
    service = AgentOrchestratorService(session, ai_router, redis_client)
    result = await service.chat(
        message=payload.user_message,
        user_id=current_user_id(current_user),
        role=current_user.get("role", "USER"),
        include_tools=False,
        plan_tier=entitlement.plan,
    )
    return GeneralAgentResponse(
        assistant_message=result["reply"],
        message_id=result["message_id"],
        why=result["why"],
        sources=result["sources"],
    )


@router.delete("/conversation", response_model=GeneralAgentConversationClearResponse)
async def clear_general_agent_conversation(
    current_user: dict = Depends(require_db_user),
    redis_client: RedisClient = Depends(get_redis_client),
) -> GeneralAgentConversationClearResponse:
    """Delete the authenticated user's cached general-agent conversation.

    All cached messages of the general agent for this user are removed,
    starting a fresh conversation on the next chat request.
    """
    cache = ConversationCache(redis_client)
    await cache.clear(GeneralAgentService.AGENT_KEY, current_user_id(current_user))
    return GeneralAgentConversationClearResponse(message="Conversa do assistente geral apagada com sucesso.")
