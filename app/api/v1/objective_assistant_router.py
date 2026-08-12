"""API router for the objective-assistant workflow."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session
from app.clients.ai_client import GroqAIClient
from app.commands.objective_assistant_command import ObjectiveAssistantCommand
from app.schemas.objective_assistant_schemas import ObjectiveAssistantRequest, ObjectiveAssistantResponse
from app.services.objective_assistant_service import ObjectiveAssistantService

router = APIRouter(prefix="/objectives/assistant", tags=["objectives"])


@router.post("", response_model=ObjectiveAssistantResponse)
async def objective_assistant(
    payload: ObjectiveAssistantRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ObjectiveAssistantResponse:
    """Run the dedicated objective-assistant flow for goal creation."""
    try:
        command = ObjectiveAssistantCommand(user_message=payload.user_message)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    ai_client = GroqAIClient()
    service = ObjectiveAssistantService(session=session, ai_client=ai_client)
    result = await service.create_completion(command, user_id=int(current_user["id"]))
    return ObjectiveAssistantResponse(**result)
