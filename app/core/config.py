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

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalize_database_url(url: str) -> str:
    """Rewrite a plain PostgreSQL URL onto the ``asyncpg`` driver scheme.

    Render's managed Postgres connection string (injected via Blueprint
    ``fromDatabase``) uses the bare ``postgresql://`` scheme.  The app's
    async engine (:mod:`app.db.session`) and Alembic
    (:mod:`migrations.env`) both require the ``postgresql+asyncpg://``
    form — the only async driver installed in production.  Normalising
    here keeps every consumer consistent and is idempotent for URLs that
    already use the asyncpg scheme.
    """
    if not url:
        return url
    for scheme in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if url.startswith(scheme):
            return f"postgresql+asyncpg://{url[len(scheme) :]}"
    return url


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
    ``secret_key`` and ``admin_default_password`` should always be provided
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
    access_token_ttl_minutes: int = 15  # Short-lived access tokens.
    refresh_token_ttl_days: int = 7  # Longer-lived refresh tokens.

    # --- Redis / caching -----------------------------------------------------
    # In production (Render, etc.) REDIS_URL and DATABASE_URL MUST be set
    # explicitly via environment variables.  The Docker-compose hostnames
    # ("redis", "postgres") only work inside a Docker network, so we gate
    # the fallback on ENVIRONMENT != "production" to get a loud failure
    # instead of a silent DNS error at runtime.
    redis_url: str = Field(
        default_factory=lambda: (
            os.getenv("REDIS_URL")
            or ("redis://redis:6379/0" if os.getenv("ENVIRONMENT", "development") != "production" else "")
        )
    )
    database_url: str | None = Field(
        default_factory=lambda: (
            os.getenv("DATABASE_URL")
            or (
                "postgresql+asyncpg://novaiax:novaiax@postgres:5432/novaiax"
                if os.getenv("ENVIRONMENT", "development") != "production"
                else ""
            )
        )
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_database_url_any_source(cls, value: str | None) -> str | None:
        """Normalise ``DATABASE_URL`` regardless of its source.

        Pydantic-settings only applies the field's ``default_factory`` when
        no env var is present, so a ``DATABASE_URL`` provided by the
        platform (e.g. Render's managed Postgres ``postgresql://`` string)
        would otherwise bypass :func:`_normalize_database_url`.  This
        validator runs for every source, keeping the URL on the asyncpg
        driver scheme the async engine requires.
        """
        return _normalize_database_url(value) if value else value

    cache_ttl_default: int = 60  # Default cache TTL in seconds.
    cache_ttl_ai: int = 300  # Cache TTL for AI responses (longer).

    # --- CORS ----------------------------------------------------------------
    allowed_origins_raw: str | None = Field(
        default=None,
        alias="ALLOWED_ORIGINS",
    )

    @property
    def allowed_origins(self) -> list[str]:
        """Accept ``ALLOWED_ORIGINS`` in either CSV or JSON form."""
        raw = self.allowed_origins_raw or os.getenv("ALLOWED_ORIGINS")
        if raw is None:
            return ["http://localhost:3000", "http://127.0.0.1:3000"]
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]

        value = str(raw).strip()
        if not value:
            return []

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [item.strip() for item in value.split(",") if item.strip()]

        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        if isinstance(parsed, str):
            return [item.strip() for item in parsed.split(",") if item.strip()]
        return [str(parsed).strip()]

    # --- API key authentication ----------------------------------------------
    api_key_header: str = "X-API-Key"

    # Legacy environment variable accepted for compatibility with older
    # deployments (it previously backed the default-admin password warn).
    # Kept only so existing environments that export ``DEFAULT_ADMIN_PASSWORD``
    # keep booting; the bootstrap and its validation now read
    # ``admin_default_password`` below.
    default_admin_password: str | None = Field(default_factory=lambda: os.getenv("DEFAULT_ADMIN_PASSWORD") or None)

    # --- Default ADMIN bootstrap account --------------------------------------
    # Used by :func:`app.db.bootstrap.ensure_default_admin` to create the first
    # administrator on startup when none exists.  ``admin_default_password`` may
    # be left unset; a strong random password is then generated and logged once
    # at creation time so operators can retrieve it from the server logs.
    admin_default_email: str = Field(default_factory=lambda: os.getenv("ADMIN_DEFAULT_EMAIL", "admin@admin.com"))
    admin_default_password: str | None = Field(default_factory=lambda: os.getenv("ADMIN_DEFAULT_PASSWORD") or None)

    # --- Admin panel -----------------------------------------------------------
    # Name of the httpOnly cookie that stores the admin panel's session (a JWT
    # access token issued by the regular login flow).
    admin_cookie_name: str = Field(default_factory=lambda: os.getenv("ADMIN_COOKIE_NAME", "novaiax_admin_session"))

    # Master API key that bypasses per-key validation.  When set, any request
    # carrying this key is treated as fully trusted.
    master_api_key: str | None = Field(default_factory=lambda: os.getenv("MASTER_API_KEY"))

    # --- Rate limiting -------------------------------------------------------
    rate_limit_default: int = 60  # Requests per minute for general endpoints.
    # Stricter limit for AI endpoints.  NOTE: this is enforced *per client IP*
    # (per user behind a trusted proxy), so it CANNOT protect the shared Groq
    # key by itself — with several users it is trivially passed in aggregate.
    # The defence against exceeding Groq's *organization-wide* quota is the
    # client-side pacer in :mod:`app.clients.ai_client` (see the ``groq_*``
    # settings below), which caps requests AND tokens per minute for the whole
    # process.  This per-IP value is kept below the provider's per-minute
    # request limit (30 RPM) with clear margin.
    rate_limit_ai: int = 8

    # Trust proxy-set client-IP headers (``X-Forwarded-For``) when
    # resolving the real client IP.  MUST only be enabled when the app sits
    # behind a trusted reverse proxy (e.g. the Render edge proxy); local
    # dev and any direct connection keep this ``False`` so client-supplied
    # headers cannot be spoofed.
    trust_proxy_headers: bool = False

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

    # --- Groq Cloud AI settings ---------------------------------------------
    groq_api_key: str | None = Field(default_factory=lambda: os.getenv("GROQ_API_KEY"))
    groq_api_base_url: str = Field(
        default_factory=lambda: os.getenv("GROQ_API_BASE_URL", "https://api.groq.com/openai/v1")
    )
    groq_api_model: str = Field(default_factory=lambda: os.getenv("GROQ_API_MODEL", "openai/gpt-oss-120b"))
    groq_api_timeout_seconds: int = Field(default_factory=lambda: int(os.getenv("GROQ_API_TIMEOUT_SECONDS", "10")))

    # --- Groq Cloud free-tier guardrails -------------------------------------
    # The free tier for ``openai/gpt-oss-120b`` (the default model) allows
    # **30 requests/min, 1K requests/day, 8K tokens/min and 200K tokens/day,
    # enforced per ORGANIZATION** — i.e. the budget is shared across every API
    # key and every worker/process on the account.  A 429 must never be escaped
    # by quick retries: every attempt, successful or not, counts towards the
    # org quota, and the per-minute window resets at most once a minute, so a
    # short backoff almost always re-hits the same exhausted window.
    #
    # The defaults below keep this process at or below the org budget *with
    # margin*:
    #   * requests: 20/min (org 30)   * tokens: 6K/min (org 8K)
    #   * concurrency: 2 (bursts queue instead of piling up)
    #   * on-429 retries: 0 (default) — no quota-multiplying silent retries.
    # These are per-process ceilings; a multi-worker deployment shares the
    # org budget, so each worker's margin is its share.
    groq_rate_limit_rpm: int = Field(default_factory=lambda: int(os.getenv("GROQ_RATE_LIMIT_RPM", "20")))
    groq_token_budget_per_minute: int = Field(
        default_factory=lambda: int(os.getenv("GROQ_TOKEN_BUDGET_PER_MINUTE", "6000"))
    )
    groq_max_concurrency: int = Field(default_factory=lambda: int(os.getenv("GROQ_MAX_CONCURRENCY", "2")))
    groq_max_attempts: int = Field(default_factory=lambda: int(os.getenv("GROQ_MAX_ATTEMPTS", "1")))
    groq_max_output_tokens: int = Field(
        default_factory=lambda: int(os.getenv("GROQ_MAX_OUTPUT_TOKENS", "1024"))
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
# Module-level configuration validation (runs on import)
# ---------------------------------------------------------------------------
# Warn if the default weak admin password is in use, so operators are
# aware before the application starts serving traffic.


def _validate_admin_password() -> None:
    """Warn if the default admin bootstrap password is unset.

    When ``ADMIN_DEFAULT_PASSWORD`` is not provided, the bootstrap creates
    the first administrator with a *randomly generated* password (retrievable
    from the server logs), so this is only a reminder — not a security error.
    """
    from app.core.logging import get_logger

    settings = get_settings()
    if not settings.admin_default_password:
        logger = get_logger(__name__)
        logger.warning(
            "ADMIN_DEFAULT_PASSWORD is not set - the first admin will be "
            "created with a random password.  Check the server logs on first "
            "startup to retrieve it, or set ADMIN_DEFAULT_PASSWORD in "
            "production to control the initial administrator password.",
        )


# ---------------------------------------------------------------------------
# Production secret / master-key validation
# ---------------------------------------------------------------------------
# Known placeholder values that must never be active in production.
_PLACEHOLDER_VALUES: set[str] = {
    "change-me-in-production",
    "changeme123",
    "replace-with-strong-key",
    "your-secret-key",
    "your-password",
    "placeholder",
}


def _validate_master_api_key_visibility() -> None:
    """Emit a warning at startup whenever a master API key is configured.

    The master key bypasses per-key validation entirely so its use must
    be auditable.  The warning is emitted on first settings load.
    """
    settings = get_settings()
    if not settings.master_api_key:
        return
    from app.core.logging import get_logger

    logger = get_logger(__name__)
    logger.warning(
        "MASTER_API_KEY is set — any request carrying this value is fully trusted and bypasses per-API-key validation.",
    )


def _validate_production_secrets() -> None:
    """Hard-fail at boot when running in production with placeholder secrets.

    When ``ENVIRONMENT == "production"``, the application raises
    :class:`RuntimeError` if any of the following hold:

    * ``SECRET_KEY`` still equals its default placeholder.
    * ``MASTER_API_KEY`` is unset or equals a known placeholder.
    * ``GROQ_API_KEY`` is unset.
    """
    settings = get_settings()
    if settings.environment != "production":
        return
    if settings.secret_key == "change-me-in-production":
        raise RuntimeError("SECRET_KEY must be set to a strong, non-default value when ENVIRONMENT=production.")
    if not settings.master_api_key or settings.master_api_key in _PLACEHOLDER_VALUES:
        raise RuntimeError("MASTER_API_KEY must be set to a strong, non-placeholder value when ENVIRONMENT=production.")
    if not settings.groq_api_key:
        raise RuntimeError("GROQ_API_KEY must be set when ENVIRONMENT=production.")
    if not settings.database_url:
        raise RuntimeError(
            "DATABASE_URL must be set when ENVIRONMENT=production. "
            "Use a PostgreSQL connection string, e.g. "
            "postgresql+asyncpg://user:pass@<database-host>:5432/dbname"
        )
    if not settings.redis_url:
        raise RuntimeError(
            "REDIS_URL must be set when ENVIRONMENT=production. "
            "Use an Upstash Redis URL, e.g. "
            "rediss://default:<password>@<endpoint>:6379"
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------
# The settings object is cached in this module-level variable so that
# ``get_settings()`` always returns the same instance after the first call.
# ---------------------------------------------------------------------------

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
        _validate_admin_password()
        _validate_master_api_key_visibility()
        _validate_production_secrets()
    return _settings
