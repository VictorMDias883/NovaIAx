"""
API v1 authentication endpoints (in-memory auth flow).

This router provides a simple authentication flow that uses the
in-memory :class:`AuthService` from :mod:`app.core.security`.  The
service is seeded with a single admin account whose credentials come
from the application settings.

Endpoints:
    - POST /auth/login   — Authenticate with username/password, receive JWT tokens.
    - POST /auth/refresh — Exchange a refresh token for a new access token.
    - GET  /auth/me      — Return the current user's identity (from the JWT).

Note: There is a parallel set of auth endpoints in
:mod:`app.api.v1.auth_router` that use a database-backed flow.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import get_auth_service
from app.core.security import AuthService
from app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse, UserResponse

# Create a sub-router with the ``/auth`` prefix and ``auth`` tag.
# The prefix is relative to the ``/api/v1`` mount point in ``main.py``.
router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, auth_service: AuthService = Depends(get_auth_service)) -> TokenResponse:
    """Exchange a refresh token for a new pair of JWT tokens.

    The provided refresh token is decoded and verified.  If it is valid
    and has ``type == "refresh"``, a new access token and refresh token
    are issued.

    Args:
        payload: Request body containing the ``refresh_token``.
        auth_service: The :class:`AuthService` dependency.

    Returns:
        A :class:`TokenResponse` with new tokens.

    Raises:
        HTTPException(401): If the refresh token is invalid or has the
            wrong type.
    """
    try:
        decoded = auth_service.decode_token(payload.refresh_token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
    # Ensure the token is a *refresh* token, not an access token.
    if decoded.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    return TokenResponse(
        access_token=auth_service.create_access_token(decoded["sub"]),
        refresh_token=auth_service.create_refresh_token(decoded["sub"]),
    )


@router.get("/me", response_model=UserResponse)
async def me(request: Request, auth_service: AuthService = Depends(get_auth_service)) -> UserResponse:
    """Return the current user's identity.

    The JWT is extracted from the ``Authorization: Bearer <token>``
    header, decoded, and the ``sub`` claim is returned as the username.

    Args:
        request: The incoming :class:`Request` (used to read headers).
        auth_service: The :class:`AuthService` dependency.

    Returns:
        A :class:`UserResponse` with the username and roles.

    Raises:
        HTTPException(401): If no valid Bearer token is provided.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = auth_header.split(" ", 1)[1]
    try:
        payload = auth_service.decode_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    return UserResponse(username=payload["sub"], roles=[])
