"""
API v1 router for the device-command agent.

The agent accepts a natural-language request and answers with a reply plus
at most one whitelisted device action.  The backend only *validates* and
returns the action — the mobile client is the component that performs it, and
for the actions flagged ``requires_confirmation`` it must first ask the user.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_router, get_entitlement, get_session, require_db_user
from app.clients.ai_router import AIRouter
from app.commands.device_action_command import DeviceActionCommand
from app.schemas.device_command_schemas import DeviceCommandRequest, DeviceCommandResponse
from app.services.device_action_service import DeviceActionService
from app.services.subscription_service import Entitlement
from app.utils.request import current_user_id

router = APIRouter(prefix="/agents/device", tags=["agents"])


@router.post("/command", response_model=DeviceCommandResponse)
async def device_command(
    payload: DeviceCommandRequest,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    ai_router: AIRouter = Depends(get_ai_router),
    entitlement: Entitlement = Depends(get_entitlement),
) -> DeviceCommandResponse:
    """Translate the user's message into a reply and an optional action.

    The returned ``action`` is a validated, whitelisted structure; the
    backend does not execute it.  ``action`` is ``null`` when the message
    needed no action or the model returned something outside the whitelist —
    the ``reply`` is still returned in that case.

    Two actions read the caller's own objectives:

    * ``WHAT_IS_PENDING_TODAY`` is answered from the database.
    * ``MARK_DAY_COMPLETED`` is owner-checked and returned for the client to
      confirm; the day is only written once the client calls
      ``PATCH /api/v1/objectives/{objective_id}/roadmap/days/{day_id}``.

    Args:
        payload: Validated request body carrying ``user_message``.
        current_user: The authenticated identity (injected via
            :func:`require_db_user`).  Actions are performed on the
            caller's own device and read their own roadmap, so an API-key
            identity is rejected.
        session: Request-scoped database session used to resolve the
            roadmap actions.

    Returns:
        A :class:`DeviceCommandResponse` with ``reply`` and ``action``.

    Raises:
        HTTPException(401): If the request is not authenticated.
        HTTPException(403): If an API-key identity calls the endpoint.
        HTTPException(422): If the request body is invalid.
    """
    try:
        command = DeviceActionCommand(user_message=payload.user_message)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    service = DeviceActionService(session=session, ai_client=ai_router)
    result = await service.create_completion(
        command,
        user_id=current_user_id(current_user),
        role=str(current_user.get("role", "")),
        plan_tier=entitlement.plan,
    )
    return DeviceCommandResponse(**result)
