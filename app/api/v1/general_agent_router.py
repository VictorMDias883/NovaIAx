"""
API v1 router for the general agent.

The general agent is a conversational assistant that can consult the
authenticated user's objectives and report which days of the roadmap
were fulfilled.  It is now a thin wrapper over the shared orchestrator in
conversational-only mode (no tool proposals).
"""

import base64
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_router, get_entitlement, get_redis_client, get_session, require_db_user
from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient
from app.clients.ai_router import AIRouter
from app.models.chat_message import ChatAgent
from app.repositories.chat_message_repository import ChatMessageRepository
from app.schemas.general_agent_schemas import (
    GeneralAgentConversationClearResponse,
    GeneralAgentRequest,
    GeneralAgentResponse,
)
from app.services.agent_orchestrator_service import AgentOrchestratorService
from app.services.general_agent_service import GeneralAgentService
from app.services.subscription_service import Entitlement
from app.utils.image_exif import strip_metadata
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


#: Image types the vision model accepts.
_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

#: Largest photo accepted.  Groq caps base64 images at 4 MB, so the raw file
#: must stay under ~3 MB; the app already resizes before uploading.
MAX_IMAGE_BYTES = 3 * 1024 * 1024


def _sniff_image_type(data: bytes) -> str | None:
    """Return the MIME type from the file's magic bytes (never trust the header)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


@router.post("/image", response_model=GeneralAgentResponse)
async def general_agent_image(
    image: UploadFile = File(...),
    user_message: str | None = Form(default=None, max_length=2000),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    ai_router: AIRouter = Depends(get_ai_router),
    entitlement: Entitlement = Depends(get_entitlement),
) -> GeneralAgentResponse:
    """Send a photo (camera/gallery) to the general agent for analysis.

    413 when the file is too large; 415 when it is not a JPEG/PNG/WebP.
    The photo is stripped of metadata (GPS, device) before it leaves the
    server and is never stored.
    """
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="A foto é grande demais (máximo de 3 MB).")
    mime = _sniff_image_type(data)
    if mime is None or mime not in _IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Envie uma foto JPEG, PNG ou WebP.")
    clean = strip_metadata(data)
    data_url = f"data:{mime};base64,{base64.b64encode(clean).decode('ascii')}"

    user_id = current_user_id(current_user)
    message_id = uuid4().hex
    # Registered like a text turn so "Denunciar resposta" works on it too.
    await ChatMessageRepository(session).create(user_id, message_id=message_id, agent=ChatAgent.GENERAL)
    service = GeneralAgentService(session, ai_router, ConversationCache(redis_client))
    result = await service.analyze_image(
        user_id=user_id,
        image_data_url=data_url,
        question=user_message,
        plan_tier=entitlement.plan,
    )
    return GeneralAgentResponse(
        assistant_message=str(result["assistant_message"]),
        message_id=message_id,
        why=list(result["why"]),  # type: ignore[call-overload]
        sources=list(result["sources"]),  # type: ignore[call-overload]
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
