"""API router for the objective-assistant workflow."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_router, get_entitlement, get_redis_client, get_session, require_db_user
from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient
from app.clients.ai_router import AIRouter
from app.commands.objective_assistant_command import ObjectiveAssistantCommand
from app.schemas.objective_assistant_schemas import ObjectiveAssistantRequest, ObjectiveAssistantResponse
from app.services.objective_assistant_service import ObjectiveAssistantService
from app.services.subscription_service import Entitlement
from app.utils.request import current_user_id

router = APIRouter(prefix="/objectives/assistant", tags=["objectives"])


@router.post("", response_model=ObjectiveAssistantResponse)
async def objective_assistant(
    payload: ObjectiveAssistantRequest,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    ai_router: AIRouter = Depends(get_ai_router),
    entitlement: Entitlement = Depends(get_entitlement),
) -> ObjectiveAssistantResponse:
    """Run the dedicated objective-assistant flow for goal creation.

    The conversation history is kept in the cache so the assistant can
    collect all information across turns.  Once the objective is created,
    the cached conversation is deleted.
    """
    try:
        command = ObjectiveAssistantCommand(user_message=payload.user_message)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = ObjectiveAssistantService(
        session=session,
        ai_client=ai_router,
        conversation_cache=ConversationCache(redis_client),
    )
    result = await service.create_completion(
        command, user_id=current_user_id(current_user), plan_tier=entitlement.plan
    )
    return ObjectiveAssistantResponse(**result)
