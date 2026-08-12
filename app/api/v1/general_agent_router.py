"""
API v1 router for the general agent.

The general agent is a conversational assistant that can consult the
authenticated user's objectives and report which days of the roadmap
were fulfilled.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_session
from app.clients.ai_client import GroqAIClient
from app.commands.general_agent_command import GeneralAgentCommand
from app.schemas.general_agent_schemas import GeneralAgentRequest, GeneralAgentResponse
from app.services.general_agent_service import GeneralAgentService

router = APIRouter(prefix="/agents/general", tags=["agents"])


@router.post("/chat", response_model=GeneralAgentResponse)
async def general_agent_chat(
    payload: GeneralAgentRequest,
    current_user: dict = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> GeneralAgentResponse:
    """Run the general-agent flow: chat with access to the user's objectives.

    The agent receives the user's message together with a context block
    built from the user's objectives and the days of their roadmaps
    (including which ones were fulfilled).
    """
    try:
        command = GeneralAgentCommand(user_message=payload.user_message)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    ai_client = GroqAIClient()
    service = GeneralAgentService(session=session, ai_client=ai_client)
    result = await service.create_completion(command, user_id=int(current_user["id"]))
    return GeneralAgentResponse(**result)
