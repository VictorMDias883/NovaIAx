from fastapi import Depends, Header, HTTPException, Request, status

from app.core.config import get_settings
from app.core.security import AuthService
from app.cache.redis_client import RedisClient


async def get_settings_dep() -> object:
    return get_settings()


async def get_auth_service() -> AuthService:
    return AuthService()


async def get_redis_client() -> RedisClient:
    return RedisClient()


async def get_current_user(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
    authorization: str | None = Header(default=None, alias="Authorization"),
    api_key: str | None = Header(default=None, alias="x-api-key"),
) -> dict[str, object]:
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
        try:
            payload = auth_service.decode_token(token)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Invalid token") from exc
        if payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="Invalid token type")
        return {"id": payload.get("sub"), "username": payload.get("sub"), "roles": []}

    if api_key and await auth_service.authenticate_api_key(api_key):
        return {"id": "api-key", "username": "api-key", "roles": ["service"]}

    raise HTTPException(status_code=401, detail="Authentication required")
