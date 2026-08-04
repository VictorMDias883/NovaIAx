"""
Application configuration management.

This module centralises all configuration values for the NovaIAx gateway.
Settings are loaded from environment variables (with sensible defaults) and
optionally from a ``.env`` file via Pydantic's ``SettingsConfigDict``.

The :class:`Settings` class is a **singleton** — :func:`get_settings` caches
the first instance so that every part of the application reads the same
configuration without re-parsing the environment on every call.
"""

import json
import os
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DownstreamService(BaseModel):
    """Represents a downstream microservice that the gateway proxies to.

    Attributes:
        name: Human-readable identifier used as the key in the
            ``downstream_services`` dictionary (e.g. ``"ai"``).
        base_url: Root URL of the downstream service (e.g.
            ``"http://mock-ai-service:8001"``).
        timeout_seconds: Maximum time (in seconds) the gateway will wait
            for a response from this service before returning a 502.
        allowed_paths: Optional list of path prefixes that are permitted
            to be proxied to this service.  When ``None`` all paths are
            allowed.
    """

    name: str
    base_url: str
    timeout_seconds: int = 5
    allowed_paths: list[str] | None = None


class Settings(BaseSettings):
    """Top-level application settings.

    Every field has a default value, so the application can run without any
    environment configuration.  In production, sensitive values such as
    ``secret_key`` and ``default_admin_password`` should always be provided
    via environment variables.
    """

    # --- Application metadata ------------------------------------------------
    app_name: str = "novaiax-gateway"
    environment: str = "development"
    host: str = "0.0.0.0"
    port: int = 8000

    # --- Security / JWT ------------------------------------------------------
    # Secret key used to sign JWT tokens.  In production this MUST be set
    # via the ``SECRET_KEY`` environment variable.
    secret_key: str = Field(default_factory=lambda: os.getenv("SECRET_KEY", "change-me-in-production"))
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15   # Short-lived access tokens.
    refresh_token_ttl_days: int = 7      # Longer-lived refresh tokens.

    # --- Redis / caching -----------------------------------------------------
    redis_url: str = Field(default_factory=lambda: os.getenv("REDIS_URL", "redis://redis:6379/0"))
    database_url: str = Field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://novaiax:novaiax@postgres:5432/novaiax",
        )
    )
    cache_ttl_default: int = 60          # Default cache TTL in seconds.
    cache_ttl_ai: int = 300              # Cache TTL for AI responses (longer).

    # --- CORS ----------------------------------------------------------------
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )

    # --- API key authentication ----------------------------------------------
    api_key_header: str = "X-API-Key"
    default_admin_username: str = "admin"
    default_admin_password: str = Field(
        default_factory=lambda: os.getenv("DEFAULT_ADMIN_PASSWORD", "changeme123")
    )
    # Master API key that bypasses per-key validation.  When set, any request
    # carrying this key is treated as fully trusted.
    master_api_key: str | None = Field(default_factory=lambda: os.getenv("MASTER_API_KEY"))

    # --- Rate limiting -------------------------------------------------------
    rate_limit_default: int = 60   # Requests per minute for general endpoints.
    rate_limit_ai: int = 10        # Stricter limit for AI endpoints.

    # --- Payload limits ------------------------------------------------------
    max_payload_bytes: int = 1024 * 1024  # 1 MiB maximum request body size.

    # --- Downstream services -------------------------------------------------
    # JSON-encoded list of downstream service definitions.  Parsed lazily
    # into a dictionary by the :attr:`downstream_services` property.
    downstream_services_json: str = Field(
        default_factory=lambda: os.getenv(
            "DOWNSTREAM_SERVICES_JSON",
            '[{"name":"ai","base_url":"http://mock-ai-service:8001","timeout_seconds":5}]',
        )
    )

    # Pydantic-settings configuration: read from ``.env`` file, case-insensitive.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    @property
    def downstream_services(self) -> dict[str, DownstreamService]:
        """Parse ``downstream_services_json`` into a name→service mapping.

        This is a property (not a cached field) so that changes to the
        environment variable at runtime are reflected.  In practice the
        value is read once at startup.
        """
        data = json.loads(self.downstream_services_json)
        services = {}
        for item in data:
            service = DownstreamService(**item)
            services[service.name] = service
        return services


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
# The settings object is cached in this module-level variable so that
# ``get_settings()`` always returns the same instance after the first call.
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton.

    On the first call a new ``Settings`` instance is created (which reads
    environment variables and the ``.env`` file).  Subsequent calls return
    the cached instance, avoiding repeated I/O.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
