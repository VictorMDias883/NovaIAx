from fastapi import APIRouter, Depends, HTTPException, Request, status

from gateway.app.api.deps import get_auth_service
from gateway.app.core.security import AuthService
from gateway.app.schemas.auth import LoginRequest, RefreshRequest, TokenResponse, UserResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, auth_service: AuthService = Depends(get_auth_service)) -> TokenResponse:
    user = auth_service.authenticate_user(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(
        access_token=auth_service.create_access_token(user["username"]),
        refresh_token=auth_service.create_refresh_token(user["username"]),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, auth_service: AuthService = Depends(get_auth_service)) -> TokenResponse:
    try:
        decoded = auth_service.decode_token(payload.refresh_token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid refresh token") from exc
    if decoded.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    return TokenResponse(
        access_token=auth_service.create_access_token(decoded["sub"]),
        refresh_token=auth_service.create_refresh_token(decoded["sub"]),
    )


@router.get("/me", response_model=UserResponse)
async def me(request: Request, auth_service: AuthService = Depends(get_auth_service)) -> UserResponse:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = auth_header.split(" ", 1)[1]
    try:
        payload = auth_service.decode_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    return UserResponse(username=payload["sub"], roles=[])
