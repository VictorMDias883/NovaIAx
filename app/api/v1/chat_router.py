"""
API v1 router for AI chat completion requests.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session
from app.clients.ai_client import GroqAIClient
from app.commands.chat_completion_command import ChatCompletionCommand
from app.schemas.chat_schemas import ChatCompletionRequest, ChatCompletionResponse
from app.services.chat_completion_service import ChatCompletionService

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/chat", response_model=ChatCompletionResponse)
async def chat_completion(
    payload: ChatCompletionRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatCompletionResponse:
    """Create a chat completion using the configured AI provider."""
    try:
        command = ChatCompletionCommand(agent_id=payload.agent_id, user_message=payload.user_message)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    ai_client = GroqAIClient()
    service = ChatCompletionService(session=session, ai_client=ai_client)
    result = await service.create_completion(command)
    return ChatCompletionResponse(**result)
