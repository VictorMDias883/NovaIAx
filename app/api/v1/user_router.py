"""
API v1 user endpoints.

Two groups live here, split by who may call them:

* **Self-service** (``PATCH /users/me``, ``PATCH /users/me/password``) — any
  authenticated database user editing their *own* account.
* **Administration** (``GET /users/``, promote/demote/delete) — ``ADMIN``
  role only, acting on any account.

Both groups share the same :class:`~app.services.user_service.UserService`;
the split is enforced purely by the injected dependency.
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_admin_db_role, require_db_user
from app.commands.change_password_command import ChangePasswordCommand
from app.commands.delete_user_command import DeleteUserCommand
from app.commands.demote_user_command import DemoteUserCommand
from app.commands.promote_user_command import PromoteUserCommand
from app.commands.update_profile_command import UpdateProfileCommand
from app.schemas.user_schemas import (
    ActionResponse,
    PasswordChangeRequest,
    UserListResponse,
    UserProfileResponse,
    UserProfileUpdate,
)
from app.services.user_service import UserService

router = APIRouter(prefix="/users", tags=["users"])


# ---------------------------------------------------------------------------
# Self-service — available to any authenticated database user.
#
# These target the caller identified by the access token, so they are
# declared ahead of the admin routes below and are deliberately *not* gated
# on the ADMIN role.  ``require_db_user`` rejects transient API-key
# identities, which have no database row to edit.
# ---------------------------------------------------------------------------


@router.patch("/me", response_model=UserProfileResponse)
async def update_my_profile(
    payload: UserProfileUpdate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> UserProfileResponse:
    """Update the signed-in user's own profile (currently just the name).

    The e-mail is intentionally not editable here — it is the login
    identifier and the unique account key, so changing it is an identity
    change rather than a profile edit.

    Args:
        payload: Validated request body with the new ``full_name``.
        current_user: The authenticated caller's identity (injected).
        session: Database session (injected via :func:`get_session`).

    Returns:
        A :class:`UserProfileResponse` carrying the refreshed identity, in
        the same shape as ``GET /auth/me`` so a client can replace its
        cached profile with it.

    Raises:
        HTTPException(404): If the caller no longer exists.
    """
    service = UserService(session)
    result = await service.update_profile(current_user, UpdateProfileCommand(full_name=payload.full_name))
    return UserProfileResponse(**result)


@router.patch("/me/password", response_model=ActionResponse)
async def change_my_password(
    payload: PasswordChangeRequest,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ActionResponse:
    """Rotate the signed-in user's own password.

    The current password must be supplied and must verify against the
    stored hash.  On success every refresh token issued before the change
    is invalidated, so all sessions must sign in again with the new
    password.

    Args:
        payload: Validated request body with ``current_password`` and
            ``new_password``.
        current_user: The authenticated caller's identity (injected).
        session: Database session (injected via :func:`get_session`).

    Returns:
        A :class:`ActionResponse` with a success message.

    Raises:
        HTTPException(400): If the current password is wrong, or the new
            one is identical to it.  Deliberately not 401 — see
            :meth:`app.services.user_service.UserService.change_password`.
        HTTPException(404): If the caller no longer exists.
    """
    service = UserService(session)
    command = ChangePasswordCommand(
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    result = await service.change_password(current_user, command)
    return ActionResponse(**result)


# ---------------------------------------------------------------------------
# Administration — ADMIN role only.
# ---------------------------------------------------------------------------


@router.patch("/{user_id}/promote", response_model=ActionResponse)
async def promote_user(
    user_id: int,
    current_user: dict = Depends(require_admin_db_role),
    session: AsyncSession = Depends(get_session),
) -> ActionResponse:
    service = UserService(session)
    result = await service.promote(current_user, PromoteUserCommand(target_id=user_id))
    return ActionResponse(**result)


@router.patch("/{user_id}/demote", response_model=ActionResponse)
async def demote_user(
    user_id: int,
    current_user: dict = Depends(require_admin_db_role),
    session: AsyncSession = Depends(get_session),
) -> ActionResponse:
    service = UserService(session)
    result = await service.demote(current_user, DemoteUserCommand(target_id=user_id))
    return ActionResponse(**result)


@router.delete("/{user_id}", response_model=ActionResponse)
async def delete_user(
    user_id: int,
    current_user: dict = Depends(require_admin_db_role),
    session: AsyncSession = Depends(get_session),
) -> ActionResponse:
    service = UserService(session)
    result = await service.delete(current_user, DeleteUserCommand(target_id=user_id))
    return ActionResponse(**result)


@router.get("/", response_model=UserListResponse)
async def list_users(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_admin_db_role),
    session: AsyncSession = Depends(get_session),
) -> UserListResponse:
    service = UserService(session)
    result = await service.list_users(page=page, limit=limit)
    return UserListResponse(**result)
