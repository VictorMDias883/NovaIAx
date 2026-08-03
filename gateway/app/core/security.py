import os
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from gateway.app.cache.redis_client import RedisClient
from gateway.app.core.config import Settings, get_settings


def create_access_token(subject: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": subject,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(subject: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": subject,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=settings.refresh_token_ttl_days)).timestamp()),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


class AuthService:
    def __init__(self, settings: Settings | None = None, redis_client: RedisClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.redis_client = redis_client or RedisClient(self.settings)
        self._users: dict[str, dict[str, Any]] = {
            self.settings.default_admin_username: {
                "username": self.settings.default_admin_username,
                "password_hash": pwd_context.hash(self.settings.default_admin_password),
                "roles": ["admin"],
            }
        }

    def verify_password(self, plain_password: str, password_hash: str) -> bool:
        return pwd_context.verify(plain_password, password_hash)

    def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        user = self._users.get(username)
        if not user:
            return None
        if not self.verify_password(password, user["password_hash"]):
            return None
        return {"username": user["username"], "roles": user["roles"]}

    async def store_api_key_hash(self, api_key: str) -> None:
        await (await self.redis_client.get_client()).set(f"api_key:{api_key[:8]}", pwd_context.hash(api_key))

    async def authenticate_api_key(self, api_key: str) -> bool:
        if not api_key:
            return False
        if self.settings.master_api_key and api_key == self.settings.master_api_key:
            return True
        stored_hash = await (await self.redis_client.get_client()).get(f"api_key:{api_key[:8]}")
        if stored_hash and pwd_context.verify(api_key, stored_hash):
            return True
        return False

    def create_access_token(self, subject: str) -> str:
        now = datetime.now(tz=timezone.utc)
        payload = {
            "sub": subject,
            "type": "access",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=self.settings.access_token_ttl_minutes)).timestamp()),
        }
        return jwt.encode(payload, self.settings.secret_key, algorithm=self.settings.jwt_algorithm)

    def create_refresh_token(self, subject: str) -> str:
        now = datetime.now(tz=timezone.utc)
        payload = {
            "sub": subject,
            "type": "refresh",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(days=self.settings.refresh_token_ttl_days)).timestamp()),
        }
        return jwt.encode(payload, self.settings.secret_key, algorithm=self.settings.jwt_algorithm)

    def decode_token(self, token: str) -> dict[str, Any]:
        return jwt.decode(token, self.settings.secret_key, algorithms=[self.settings.jwt_algorithm])
