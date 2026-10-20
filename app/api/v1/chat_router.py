"""
API v1 router for AI chat completion requests.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_ai_router,
    get_current_user,
    get_entitlement,
    get_redis_client,
    get_session,
    require_db_user,
)
from app.cache.redis_client import RedisClient
from app.clients.ai_router import AIRouter
from app.commands.chat_completion_command import ChatCompletionCommand
from app.commands.clear_chat_conversation_command import ClearChatConversationCommand
from app.schemas.chat_schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ConversationClearResponse,
)
from app.services.chat_completion_service import ChatCompletionService
from app.services.subscription_service import Entitlement
from app.utils.request import current_user_id

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/chat", response_model=ChatCompletionResponse)
async def chat_completion(
    payload: ChatCompletionRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    ai_router: AIRouter = Depends(get_ai_router),
    entitlement: Entitlement = Depends(get_entitlement),
) -> ChatCompletionResponse:
    """Create a chat completion using the configured AI provider.

    The previous turns held with the same agent are replayed to the model, so
    the conversation keeps its context across requests.
    """
    try:
        command = ChatCompletionCommand(agent_id=payload.agent_id, user_message=payload.user_message)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = ChatCompletionService(session=session, ai_client=ai_router, cache=redis_client)
    result = await service.create_completion(
        command, user_id=current_user["id"], plan_tier=entitlement.plan
    )
    return ChatCompletionResponse(**result)


@router.delete("/chat/conversation", response_model=ConversationClearResponse)
async def clear_chat_conversation(
    agent_id: int = Query(..., gt=0),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    ai_router: AIRouter = Depends(get_ai_router),
) -> ConversationClearResponse:
    """Forget the caller's conversation history with one agent.

    Only the authenticated user's own thread is dropped; other agents and other
    users are untouched.
    """
    try:
        command = ClearChatConversationCommand(agent_id=agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = ChatCompletionService(session=session, ai_client=ai_router, cache=redis_client)
    await service.clear_conversation(command, user_id=current_user_id(current_user))
    return ConversationClearResponse(agent_id=agent_id)
