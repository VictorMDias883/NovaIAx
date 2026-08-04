"""
Security utilities: JWT token creation/verification and password hashing.

This module provides two layers of security functionality:

1. **Module-level functions** (``create_access_token``, ``create_refresh_token``,
   ``decode_token``) — stateless helpers that operate on the global
   :class:`Settings` singleton.  These are used by the database-backed
   :class:`AuthService` in :mod:`app.services.auth_service`.

2. **The :class:`AuthService` class** — a higher-level service that wraps
   token creation/decoding and adds:
   - An in-memory user store (seeded with a default admin account).
   - Password hashing and verification via ``passlib``.
   - API-key authentication backed by Redis (or an in-memory fallback).

The module-level functions and the class methods duplicate some logic
(token creation/decoding).  The class methods are preferred when a
specific :class:`Settings` or :class:`RedisClient` instance is needed;
the module-level functions are convenient for one-off use.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from app.cache.redis_client import RedisClient
from app.core.config import Settings, get_settings


# ---------------------------------------------------------------------------
# Module-level JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(subject: str, settings: Settings | None = None) -> str:
    """Create a short-lived JWT **access token**.

    The token contains:
        - ``sub``: the subject (typically the user ID).
        - ``type``: always ``"access"`` (used to distinguish from refresh tokens).
        - ``iat``: issued-at timestamp (UTC).
        - ``exp``: expiration timestamp (UTC), ``access_token_ttl_minutes``
          minutes from now.

    Args:
        subject: The entity the token represents (usually a user ID).
        settings: Optional :class:`Settings` instance.  If omitted, the
            global singleton is used.

    Returns:
        A compact JWT string signed with the configured secret key.
    """
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
    """Create a long-lived JWT **refresh token**.

    Structurally identical to :func:`create_access_token` but with
    ``type`` set to ``"refresh"`` and a TTL of ``refresh_token_ttl_days``
    days instead of minutes.

    Refresh tokens are used to obtain new access tokens without requiring
    the user to re-authenticate.
    """
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
    """Verify and decode a JWT token.

    Raises ``jwt.JWTError`` (or a subclass) if the token is expired,
    has an invalid signature, or is otherwise malformed.

    Args:
        token: The JWT string to decode.
        settings: Optional :class:`Settings` instance.

    Returns:
        The token payload as a dictionary.
    """
    settings = settings or get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])


# ---------------------------------------------------------------------------
# Password hashing context
# ---------------------------------------------------------------------------
# ``passlib`` provides a unified interface for password hashing.  We use
# PBKDF2 with SHA-256, which is built into Python's standard library
# (no external C dependencies).  The ``deprecated="auto"`` flag tells
# passlib to automatically upgrade hashes that use older schemes.
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


# ---------------------------------------------------------------------------
# AuthService — higher-level security service
# ---------------------------------------------------------------------------

class AuthService:
    """Service layer for authentication and token management.

    Unlike the module-level functions, this class maintains an in-memory
    user store and integrates with Redis for API-key validation.  It is
    used by the API v1 auth endpoints (``app.api.v1.auth``) and the
    dependency-injection layer (``app.api.deps``).

    The in-memory user store is seeded with a single admin account whose
    credentials come from :class:`Settings`.  In a production system this
    would be replaced by a database-backed user repository.
    """

    def __init__(self, settings: Settings | None = None, redis_client: RedisClient | None = None) -> None:
        """Initialise the service with settings and a Redis client.

        Args:
            settings: Optional :class:`Settings` instance (defaults to the
                global singleton).
            redis_client: Optional :class:`RedisClient` instance.  If not
                provided, a new one is created.
        """
        self.settings = settings or get_settings()
        self.redis_client = redis_client or RedisClient(self.settings)
        # In-memory user store.  In production this would be a database.
        self._users: dict[str, dict[str, Any]] = {
            self.settings.default_admin_username: {
                "username": self.settings.default_admin_username,
                "password_hash": pwd_context.hash(self.settings.default_admin_password),
                "roles": ["admin"],
            }
        }

    def verify_password(self, plain_password: str, password_hash: str) -> bool:
        """Check a plaintext password against a stored hash.

        Uses constant-time comparison internally to mitigate timing
        attacks.
        """
        return pwd_context.verify(plain_password, password_hash)

    def authenticate_user(self, username: str, password: str) -> dict[str, Any] | None:
        """Authenticate a user by username and password.

        Returns a dict with ``username`` and ``roles`` on success, or
        ``None`` if the user does not exist or the password is wrong.
        """
        user = self._users.get(username)
        if not user:
            return None
        if not self.verify_password(password, user["password_hash"]):
            return None
        return {"username": user["username"], "roles": user["roles"]}

    async def store_api_key_hash(self, api_key: str) -> None:
        """Store a hashed API key in Redis.

        Only the first 8 characters of the key are used as the Redis key
        (a prefix), while the full key is hashed with PBKDF2 before
        storage.  This allows lookups by prefix without storing the raw
        key.
        """
        await (await self.redis_client.get_client()).set(f"api_key:{api_key[:8]}", pwd_context.hash(api_key))

    async def authenticate_api_key(self, api_key: str) -> bool:
        """Validate an API key.

        Two checks are performed:
        1. If a ``master_api_key`` is configured and the provided key
           matches it, authentication succeeds immediately.
        2. Otherwise, the key's prefix is used to look up a stored hash
           in Redis, and the full key is verified against that hash.

        Returns ``True`` if the key is valid, ``False`` otherwise.
        """
        if not api_key:
            return False
        if self.settings.master_api_key and api_key == self.settings.master_api_key:
            return True
        stored_hash = await (await self.redis_client.get_client()).get(f"api_key:{api_key[:8]}")
        if stored_hash and pwd_context.verify(api_key, stored_hash):
            return True
        return False

    def create_access_token(self, subject: str) -> str:
        """Create an access token using this service's settings.

        This is an instance method wrapper around the module-level
        :func:`create_access_token`, using ``self.settings``.
        """
        now = datetime.now(tz=timezone.utc)
        payload = {
            "sub": subject,
            "type": "access",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=self.settings.access_token_ttl_minutes)).timestamp()),
        }
        return jwt.encode(payload, self.settings.secret_key, algorithm=self.settings.jwt_algorithm)

    def create_refresh_token(self, subject: str) -> str:
        """Create a refresh token using this service's settings.

        Instance-method counterpart to :func:`create_refresh_token`.
        """
        now = datetime.now(tz=timezone.utc)
        payload = {
            "sub": subject,
            "type": "refresh",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(days=self.settings.refresh_token_ttl_days)).timestamp()),
        }
        return jwt.encode(payload, self.settings.secret_key, algorithm=self.settings.jwt_algorithm)

    def decode_token(self, token: str) -> dict[str, Any]:
        """Decode and verify a JWT token using this service's settings.

        Instance-method counterpart to :func:`decode_token`.
        """
        return jwt.decode(token, self.settings.secret_key, algorithms=[self.settings.jwt_algorithm])
