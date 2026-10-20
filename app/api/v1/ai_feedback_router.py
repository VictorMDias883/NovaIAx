"""API v1 router for reporting AI answers (offensive/wrong/unsafe)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_db_user
from app.repositories.ai_feedback_repository import AIFeedbackRepository
from app.repositories.chat_message_repository import ChatMessageRepository
from app.schemas.ai_feedback_schemas import AIFeedbackRequest, AIFeedbackResponse
from app.utils.request import current_user_id

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/feedback", response_model=AIFeedbackResponse, status_code=201)
async def submit_feedback(
    payload: AIFeedbackRequest,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> AIFeedbackResponse:
    """Record a user report about one AI answer.

    ``message_id`` is the value returned by the agent chat response.  It is
    validated against the caller's own turns first, so a report can only be
    filed against a message the caller actually received.

    Raises:
        HTTPException(404): If ``message_id`` was not issued to the caller.
    """
    user_id = current_user_id(current_user)
    if not await ChatMessageRepository(session).exists_for_user(user_id, payload.message_id):
        raise HTTPException(status_code=404, detail="Mensagem não encontrada")
    feedback = await AIFeedbackRepository(session).create(
        user_id,
        message_id=payload.message_id,
        reason=payload.reason.value,
        details=payload.details,
    )
    return AIFeedbackResponse.model_validate(feedback)
