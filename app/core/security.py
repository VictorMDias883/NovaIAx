"""
Security utilities: JWT token creation/verification, password hashing,
and API-key authentication.

This module provides:

1. **JWT helpers** (``create_access_token``, ``create_refresh_token``,
   ``decode_token``) — stateless module-level functions that operate on
   the global :class:`Settings` singleton.  Used by the database-backed
   auth service and the API-key service.

2. **``pwd_context``** — a shared ``passlib`` hashing context (PBKDF2
   with SHA-256).  Kept here so every consumer (auth service, API-key
   service) uses the same configuration.

3. **:class:`ApiKeyService`** — validates/hashes API keys against Redis
   (with an in-memory fallback), including support for a master key that
   bypasses validation.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from jose import jwt
from passlib.context import CryptContext

from app.cache.redis_client import RedisClient
from app.core.config import Settings, get_settings

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(
    subject: str,
    email: str | None = None,
    role: str | None = None,
    settings: Settings | None = None,
) -> str:
    """Create a short-lived JWT **access token**.

    The token contains:
        - ``sub``: The subject (typically the user ID).
        - ``email``: The user's email.
        - ``role``: The user's current RBAC role at login time.
        - ``type``: always ``"access"``.
        - ``jti``: A unique token ID (used for revocation on logout).
        - ``iat``: issued-at timestamp (UTC).
        - ``exp``: expiration timestamp (UTC), ``access_token_ttl_minutes``
          minutes from now.

    Args:
        subject: The entity the token represents (usually a user ID).
        email: Optional email to include in the token payload.
        role: Optional role to include in the token payload.
        settings: Optional :class:`Settings` instance.

    Returns:
        A compact JWT string signed with the configured secret key.
    """
    settings = settings or get_settings()
    now = datetime.now(tz=UTC)
    payload = {
        "sub": subject,
        "type": "access",
        "jti": uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_ttl_minutes)).timestamp()),
    }
    if email is not None:
        payload["email"] = email
    if role is not None:
        payload["role"] = role
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(
    subject: str,
    settings: Settings | None = None,
) -> str:
    """Create a long-lived JWT **refresh token**.

    Structurally identical to :func:`create_access_token` but with
    ``type`` set to ``"refresh"``, a unique ``jti``, and a TTL of
    ``refresh_token_ttl_days`` days instead of minutes.
    """
    settings = settings or get_settings()
    now = datetime.now(tz=UTC)
    payload = {
        "sub": subject,
        "type": "refresh",
        "jti": uuid4().hex,
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
# API-key service
# ---------------------------------------------------------------------------

class ApiKeyService:
    """Validate and manage API keys backed by Redis.

    API keys are never stored in plain text: only a PBKDF2 hash of the
    key is kept, addressed by an 8-character prefix (allowing lookups
    by prefix without leaking the key itself).
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

    async def authenticate_api_key(self, api_key: str) -> bool:
        """Validate an API key.

        Two checks are performed:
        1. If a ``master_api_key`` is configured and the provided key
           matches it, authentication succeeds immediately.
        2. Otherwise, the key's prefix is used to look up a stored hash
           in Redis, and the full key is verified against that hash.

        Returns ``True`` if the key is valid, ``False`` otherwise.
        """
        # Never accept an empty or whitespace-only key.  The truthiness of
        # ``master_key`` below also guarantees an empty configured master
        # key can never silently match any request.
        if not api_key or not api_key.strip():
            return False
        master_key = self.settings.master_api_key
        if master_key and api_key == master_key:
            return True
        client = await self.redis_client.get_client()
        stored_hash = await client.get(f"api_key:{api_key[:8]}")
        return bool(stored_hash and pwd_context.verify(api_key, stored_hash))
