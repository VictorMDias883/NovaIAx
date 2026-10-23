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
import re

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back to ``default``.

    A malformed value (empty string, typo, leftover ``"none"``) must not
    crash the process at import time — :class:`Settings` is built during
    module import of :mod:`app.db.session`, so raising here would take the
    whole application down with a bare ``ValueError`` instead of a
    readable configuration error.  The bad value is therefore ignored in
    favour of the documented default.
    """
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back to ``default``.

    See :func:`_env_float` for why an unparsable value must not raise.
    """
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean from the environment, falling back to ``default``.

    Accepts the usual truthy spellings (``1``/``true``/``yes``/``on``); any
    other value — including a typo — is treated as ``False``.  As with
    :func:`_env_float`, a malformed value must never crash the process at
    import time.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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


class AIProviderConfig(BaseModel):
    """Configuration for one OpenAI-compatible AI provider.

    Every provider in this project speaks the OpenAI chat-completions
    protocol (``POST /chat/completions`` carrying ``{"model", "messages"}``
    and a ``Bearer`` API key), so a single generic client can talk to any of
    them.  Providers are declared either through ``AI_PROVIDERS_JSON`` (for
    full control) or built from the well-known ``groq``/``openai``/``openrouter``
    environment groups (see :attr:`Settings.ai_providers`).

    Attributes:
        name: Stable identifier used in logs and responses (e.g. ``"groq"``).
        base_url: Root of the OpenAI-compatible API (no trailing slash).
        api_key: Provider credential.  A provider without a key is skipped
            by the router instead of failing the request.
        default_model: Fallback model used when no ``models`` entry matches
            the request's plan tier and task type.
        models: Optional ``selection_key -> model`` mapping.  Keys are
            ``"<tier>:<task>"``, ``"<tier>"``, ``"<task>"`` or ``"default"``
            (first match wins), e.g. ``{"chat": "llama-3.3-70b",
            "strict_json": "openai/gpt-oss-120b"}``.
        priority: Lower values are tried first by :class:`AIRouter`.
        enabled: ``False`` removes the provider from routing entirely.
        timeout_seconds: Per-request HTTP timeout for this provider.
        rpm: Requests-per-minute ceiling for the process-wide pacer shared
            by this provider's client.
        tpm: Tokens-per-minute ceiling for the same pacer.
        max_concurrency: Cap on simultaneous in-flight requests per provider.
        max_attempts: How many times an individual request is retried against
            *this* provider before the router falls back to the next one.
        max_output_tokens: ``max_tokens`` cap applied to every response.
        breaker_failure_threshold: Consecutive failures that trip the circuit
            breaker.
        breaker_cooldown_seconds: How long the provider is skipped once its
            breaker is open.
    """

    name: str
    base_url: str
    api_key: str | None = None
    default_model: str = "openai/gpt-oss-120b"
    models: dict[str, str] = Field(default_factory=dict)
    priority: int = 100
    enabled: bool = True
    timeout_seconds: int = 25
    rpm: int = 20
    tpm: int = 6000
    max_concurrency: int = 2
    max_attempts: int = 1
    max_output_tokens: int = 1024
    breaker_failure_threshold: int = 3
    breaker_cooldown_seconds: float = 30.0


class Settings(BaseSettings):
    """Top-level application settings.

    Every field has a default value, so the application can run without any
    environment configuration.  In production, sensitive values such as
    ``secret_key`` and ``admin_bootstrap_password`` should always be provided
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

    # --- Community media ----------------------------------------------------
    # Uploaded community-report photos are stored with their EXIF/metadata
    # segments stripped (see ``app.utils.image_exif``).  The directory is
    # created on first write; the size cap rejects oversized uploads before
    # any parsing happens.
    community_media_dir: str = os.getenv("COMMUNITY_MEDIA_DIR", "var/community_media")
    community_max_upload_bytes: int = _env_int("COMMUNITY_MAX_UPLOAD_BYTES", 5 * 1024 * 1024)
    #: Number of user flags that auto-hides a community report (its
    #: ``hidden_at`` is stamped and a moderator can restore it).  ``0`` (or
    #: less) disables auto-hiding entirely.
    community_report_flag_threshold: int = _env_int("COMMUNITY_REPORT_FLAG_THRESHOLD", 5)

    # --- Database connection pool -------------------------------------------
    # ``app.db.session`` applies these to the async engine.  The defaults are
    # sized for a small container (Render free tier / single worker): 10
    # long-lived connections plus a burst allowance of 10 more, which is far
    # more than the handful of concurrent requests a mobile app generates
    # while still staying inside Postgres' default ``max_connections=100``.
    #
    # ``pool_pre_ping`` issues a cheap ``SELECT 1`` before a connection is
    # handed out, so a connection killed by the provider (or an idle timeout
    # at a load balancer) is transparently discarded instead of surfacing to
    # the user as a 500 on the first query.
    #
    # ``pool_recycle`` proactively retires connections that have been idle
    # long enough to have been dropped by the provider/firewall, avoiding the
    # "server closed the connection unexpectedly" error in the middle of a
    # request.  30 minutes is comfortably below the 5-minute idle cutoff used
    # by most managed-Postgres proxies.
    db_pool_size: int = _env_int("DB_POOL_SIZE", 10)
    db_max_overflow: int = _env_int("DB_MAX_OVERFLOW", 10)
    db_pool_recycle_seconds: int = _env_int("DB_POOL_RECYCLE_SECONDS", 1800)
    db_pool_timeout_seconds: float = _env_float("DB_POOL_TIMEOUT_SECONDS", 10.0)
    db_pool_pre_ping: bool = os.getenv("DB_POOL_PRE_PING", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

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
    # It is the oldest spelling of the bootstrap password and is folded into
    # ``admin_bootstrap_password`` by the alias resolver below.
    default_admin_password: str | None = Field(default_factory=lambda: os.getenv("DEFAULT_ADMIN_PASSWORD") or None)

    # --- Operator (admin) bootstrap account -----------------------------------
    # The operator console (``/admin``) has no registration endpoint: the
    # account is created by the API itself, on every boot, when **both**
    # ``ADMIN_BOOTSTRAP_EMAIL`` and ``ADMIN_BOOTSTRAP_PASSWORD`` are set (see
    # :func:`app.db.bootstrap.bootstrap_admin_account`).
    #
    # The password is applied **only** when the account is created — an
    # existing account keeps its own password forever, so the variables can be
    # removed from the environment right after the first deploy.
    admin_bootstrap_email: str | None = None
    admin_bootstrap_password: str | None = None
    # Display name stored on the bootstrapped account.
    admin_bootstrap_name: str = "Administrador"
    # Promote an existing (non-admin) account with the same e-mail to ADMIN.
    # Off by default: an existing account is never touched.
    admin_bootstrap_promote_existing: bool = False

    @field_validator("admin_bootstrap_promote_existing", mode="before")
    @classmethod
    def _coerce_promote_existing(cls, value: object) -> bool:
        """Accept the usual truthy spellings; anything else means "off".

        A typo must never crash the boot (the settings are built at import
        time), so an unparsable value falls back to ``False`` — the safe
        default, which leaves existing accounts untouched.
        """
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    # Deprecated aliases of the bootstrap variables above.  They keep working
    # (an existing deployment must not lose its operator) but they log one
    # warning on startup — see :func:`_validate_admin_bootstrap`.
    admin_default_email: str | None = Field(default_factory=lambda: os.getenv("ADMIN_DEFAULT_EMAIL") or None)
    admin_default_password: str | None = Field(default_factory=lambda: os.getenv("ADMIN_DEFAULT_PASSWORD") or None)

    #: Names of the deprecated variables that actually supplied a value.
    #: Kept in a **private attribute** (and exposed read-only through
    #: :attr:`admin_bootstrap_deprecated_env`) on purpose: a model *field*
    #: would be read from the environment, and pydantic-settings parses such
    #: a value as JSON — a stray ``ADMIN_BOOTSTRAP_DEPRECATED_ENV`` in the
    #: environment would then break the boot with a parse error.
    _admin_bootstrap_deprecated_env: list[str] = PrivateAttr(default_factory=list)

    @property
    def admin_bootstrap_deprecated_env(self) -> list[str]:
        """Names of the deprecated variables that actually supplied a value.

        Empty when the canonical ``ADMIN_BOOTSTRAP_*`` variables were used.
        """
        return list(self._admin_bootstrap_deprecated_env)

    @model_validator(mode="after")
    def _resolve_deprecated_admin_aliases(self) -> "Settings":
        """Fold the deprecated ``ADMIN_DEFAULT_*`` names into the new ones.

        ``ADMIN_DEFAULT_EMAIL`` / ``ADMIN_DEFAULT_PASSWORD`` (and the older
        ``DEFAULT_ADMIN_PASSWORD``) keep working as aliases of
        ``ADMIN_BOOTSTRAP_EMAIL`` / ``ADMIN_BOOTSTRAP_PASSWORD``.  The names
        that were actually used are recorded in
        :attr:`admin_bootstrap_deprecated_env` so the startup can warn once.
        """
        deprecated: list[str] = []
        if not self.admin_bootstrap_email and self.admin_default_email:
            self.admin_bootstrap_email = self.admin_default_email
            deprecated.append("ADMIN_DEFAULT_EMAIL")
        if not self.admin_bootstrap_password:
            if self.admin_default_password:
                self.admin_bootstrap_password = self.admin_default_password
                deprecated.append("ADMIN_DEFAULT_PASSWORD")
            elif self.default_admin_password:
                self.admin_bootstrap_password = self.default_admin_password
                deprecated.append("DEFAULT_ADMIN_PASSWORD")
        self._admin_bootstrap_deprecated_env = deprecated
        return self

    # --- Admin panel -----------------------------------------------------------
    # The operator console is a first-class surface, authenticated separately
    # from the mobile API.  Admins are never registered through the panel: a
    # row with ``role='ADMIN'`` is inserted directly in the database (Supabase
    # SQL editor) — see ``scripts/make_admin_sql.py``.  The panel login accepts
    # only accounts whose **live** database role is ``ADMIN``.

    # Master switch.  When False every ``/admin`` route answers 404 so an
    # operator can disable the console without removing it from the app.
    admin_panel_enabled: bool = _env_bool("ADMIN_PANEL_ENABLED", True)

    # Name of the httpOnly cookie that stores the panel's opaque session id.
    # Distinct from the mobile API cookie so the two sessions never mix.
    admin_panel_cookie_name: str = Field(
        default_factory=lambda: os.getenv("ADMIN_PANEL_COOKIE_NAME", "novaiax_admin_panel")
    )

    # Legacy cookie name kept only so an in-flight session survives the rename;
    # accepted as a fallback while reading, never written.
    admin_cookie_name: str = Field(default_factory=lambda: os.getenv("ADMIN_COOKIE_NAME", "novaiax_admin_session"))

    # Optional second factor.  When True, an admin whose account has a TOTP
    # secret must present a valid code to log in, and an admin without one is
    # forced through enrolment before reaching any other page.
    admin_require_totp: bool = _env_bool("ADMIN_REQUIRE_TOTP", False)
    admin_totp_issuer: str = Field(default_factory=lambda: os.getenv("ADMIN_TOTP_ISSUER", "NovaIAx Admin"))

    # Optional source-IP allowlist (CSV).  Empty means "any IP".  Compared
    # against the resolved client IP (X-Forwarded-For only when
    # ``trust_proxy_headers`` is on).
    admin_allowed_ips_raw: str | None = Field(default_factory=lambda: os.getenv("ADMIN_ALLOWED_IPS"))

    # Session lifetimes: idle timeout (no request) and absolute cap.
    admin_session_idle_minutes: int = _env_int("ADMIN_SESSION_IDLE_MINUTES", 60)
    admin_session_absolute_hours: int = _env_int("ADMIN_SESSION_ABSOLUTE_HOURS", 8)

    # --- Agent catalog / prompt console --------------------------------------
    # Hard cap on any admin-authored prompt fragment (agent groups, sub-agents
    # and system prompts).  Enforced on save so a prompt cannot blow up the
    # context window; the deny-list linter blocks attempts to override the
    # immutable safety preamble (see ``app.clients.prompt_safety``).
    agent_prompt_max_chars: int = _env_int("AGENT_PROMPT_MAX_CHARS", 5000)
    # The "Testar agente" sandbox skips the end-user daily quota (it never
    # touches a real user) but is rate-limited per admin to protect the shared
    # AI providers.
    agent_preview_rate_limit: int = _env_int("AGENT_PREVIEW_RATE_LIMIT", 10)
    agent_preview_rate_window_seconds: int = _env_int("AGENT_PREVIEW_RATE_WINDOW_SECONDS", 60)

    # --- Promotions / promo codes -------------------------------------------
    # Redeeming a code is a cheap but abusable endpoint, so it is rate-limited
    # both per user and per source IP (a user rotating IPs cannot amplify its
    # trial/quota grants, and one IP cannot brute-force code guesses).
    promo_redeem_user_limit: int = _env_int("PROMO_REDEEM_USER_LIMIT", 10)
    promo_redeem_ip_limit: int = _env_int("PROMO_REDEEM_IP_LIMIT", 30)
    promo_redeem_window_seconds: int = _env_int("PROMO_REDEEM_WINDOW_SECONDS", 60)

    # --- Config center (remote app config, plan overrides, AI ops) -----------
    # The public ``GET /api/v1/app-config`` payload is cached in Redis for this
    # many seconds; a panel change busts the cache immediately.
    app_config_cache_ttl_seconds: int = _env_int("APP_CONFIG_CACHE_TTL_SECONDS", 60)
    # How long a worker may reuse the in-memory plan-ceiling override snapshot
    # before refreshing it from the database.
    plan_limit_override_ttl_seconds: int = _env_int("PLAN_LIMIT_OVERRIDE_TTL_SECONDS", 30)
    # How many days of per-plan AI usage aggregates are retained in Redis.
    ai_usage_retention_days: int = _env_int("AI_USAGE_RETENTION_DAYS", 35)
    # Size of the per-provider rolling latency/error windows used by the
    # "Operações de IA" panel page.
    ai_metrics_window: int = _env_int("AI_METRICS_WINDOW", 200)

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
    rate_limit_ai: int = 10

    # Stricter limit for AI endpoints, applied **per authenticated user id**
    # rather than per client IP.  Keying on the user means a single account
    # cannot amplify its share of the shared provider quota by rotating IPs,
    # and several users behind one NAT/office IP each get their own budget.
    # Only authenticated AI routes are bucketed this way; unauthenticated
    # requests keep the per-IP ``rate_limit_ai`` bucket.
    rate_limit_ai_user: int = _env_int("RATE_LIMIT_AI_USER", 8)

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
    groq_api_timeout_seconds: int = _env_int("GROQ_API_TIMEOUT_SECONDS", 25)

    # --- Groq request budget (keeps AI routes under 60s) ---------------------
    # A request to an AI route can wait twice: once in the process-wide pacer
    # queue (waiting for an RPM/token slot) and once on the HTTP call itself.
    # Both waits are bounded so the *total* stays below the budget the mobile
    # app waits for, instead of the request hanging until the client gives up.
    #
    #   * ``groq_queue_wait_seconds`` caps how long a request may sit in the
    #     pacer/semaphore queue before giving up with a 429.
    #   * ``groq_request_budget_seconds`` is the hard ceiling for the whole
    #     operation; ``app.clients.ai_client`` enforces it and derives the
    #     queue wait from it, so the two can never drift apart.
    #
    # Defaults: 25s queueing + 25s of HTTP time = 50s worst case, leaving 10s
    # of headroom inside the 60s target.
    groq_request_budget_seconds: float = _env_float("GROQ_REQUEST_BUDGET_SECONDS", 60.0)
    groq_queue_wait_seconds: float = _env_float("GROQ_QUEUE_WAIT_SECONDS", 25.0)

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
    groq_rate_limit_rpm: int = _env_int("GROQ_RATE_LIMIT_RPM", 20)
    groq_token_budget_per_minute: int = _env_int("GROQ_TOKEN_BUDGET_PER_MINUTE", 6000)
    groq_max_concurrency: int = _env_int("GROQ_MAX_CONCURRENCY", 2)
    groq_max_attempts: int = _env_int("GROQ_MAX_ATTEMPTS", 1)
    groq_max_output_tokens: int = _env_int("GROQ_MAX_OUTPUT_TOKENS", 1024)

    # --- OpenAI-compatible provider fleet --------------------------------
    # Since every provider speaks the same chat-completions protocol, extra
    # providers are plain configuration.  ``AI_PROVIDERS_JSON`` declares the
    # whole fleet explicitly (for operators who need models/keyed routing);
    # otherwise well-known providers are derived from their own env groups:
    #
    #   * Groq from the ``groq_*`` settings above (backwards compatible);
    #   * OpenAI from ``OPENAI_API_KEY`` / ``OPENAI_API_BASE_URL`` /
    #     ``OPENAI_API_MODEL``;
    #   * OpenRouter from ``OPENROUTER_API_KEY`` / ``OPENROUTER_API_BASE_URL``
    #     / ``OPENROUTER_API_MODEL``.
    #
    # A provider without a matching API key is simply absent from the fleet,
    # so the router falls back to whatever *is* configured.
    ai_providers_json: str | None = Field(default_factory=lambda: os.getenv("AI_PROVIDERS_JSON"))
    openai_api_key: str | None = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY"))
    openai_api_base_url: str = Field(
        default_factory=lambda: os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1")
    )
    openai_api_model: str = Field(default_factory=lambda: os.getenv("OPENAI_API_MODEL", "gpt-4o-mini"))
    openrouter_api_key: str | None = Field(default_factory=lambda: os.getenv("OPENROUTER_API_KEY"))
    openrouter_api_base_url: str = Field(
        default_factory=lambda: os.getenv("OPENROUTER_API_BASE_URL", "https://openrouter.ai/api/v1")
    )
    openrouter_api_model: str = Field(
        default_factory=lambda: os.getenv("OPENROUTER_API_MODEL", "openai/gpt-4o-mini")
    )

    # --- Per-user daily quota defaults -----------------------------------
    # Until the plans feature lands, every user shares these sane defaults.
    # :func:`app.clients.ai_quota.get_plan_limits` is the single seam where a
    # real per-tier plan lookup will later replace these constants.
    ai_default_daily_messages: int = _env_int("AI_DEFAULT_DAILY_MESSAGES", 200)
    ai_default_daily_tokens: int = _env_int("AI_DEFAULT_DAILY_TOKENS", 50_000)
    ai_default_max_output_tokens: int = _env_int("AI_DEFAULT_MAX_OUTPUT_TOKENS", 1024)
    ai_breaker_failure_threshold: int = _env_int("AI_BREAKER_FAILURE_THRESHOLD", 3)
    ai_breaker_cooldown_seconds: float = _env_float("AI_BREAKER_COOLDOWN_SECONDS", 30.0)

    # --- Google Play billing -------------------------------------------------
    # Subscription purchases are verified server-side against the Google Play
    # Developer API; the client's purchase token is proof only of *what* to
    # verify, never of entitlement.  Every field below is optional so the app
    # runs in development without Play credentials: verification simply
    # returns a clear "billing not configured" error instead of crashing.
    #
    #   * ``google_play_package_name`` is the Android application id.
    #   * ``google_play_service_account_json`` is the service-account key (the
    #     full JSON, inline) with the ``androidpublisher`` scope granted in the
    #     Play Console — treat it as a secret.
    #   * ``google_play_products_json`` maps plan names to Play product ids.
    #   * ``google_play_rtdn_token`` guards the Pub/Sub push endpoint; the
    #     optional ``google_play_rtdn_audience`` enables OIDC verification.
    #   * ``billing_free_trial_days`` is how long a brand-new account keeps the
    #     full TRIAL experience before dropping to the limited FREE tier.
    google_play_package_name: str = Field(
        default_factory=lambda: os.getenv("GOOGLE_PLAY_PACKAGE_NAME", "br.com.novaiax.agente")
    )
    google_play_service_account_json: str | None = Field(
        default_factory=lambda: os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON")
    )
    google_play_products_json: str = Field(
        default_factory=lambda: os.getenv(
            "GOOGLE_PLAY_PRODUCTS_JSON",
            '{"starter": "novaiax_starter", "pro": "novaiax_pro", "premium": "novaiax_premium"}',
        )
    )
    google_play_rtdn_token: str | None = Field(
        default_factory=lambda: os.getenv("GOOGLE_PLAY_RTDN_TOKEN")
    )
    google_play_rtdn_audience: str | None = Field(
        default_factory=lambda: os.getenv("GOOGLE_PLAY_RTDN_AUDIENCE")
    )
    billing_free_trial_days: int = _env_int("BILLING_FREE_TRIAL_DAYS", 7)
    billing_acknowledge_purchases: bool = _env_bool("BILLING_ACKNOWLEDGE_PURCHASES", True)

    # --- Privacy, consent and age gate ---------------------------------------
    # Versions of the consent docs a user can accept.  ``consent_log`` rows
    # store the version so a future rewrite ("v2 terms") is auditable; new
    # registrations auto-record ``terms`` and ``privacy`` with these versions.
    consent_versions_json: str = Field(
        default_factory=lambda: os.getenv(
            "CONSENT_VERSIONS_JSON",
            '{"terms": "1", "privacy": "1", "microphone": "1", '
            '"contacts": "1", "location": "1", "learning": "1"}',
        )
    )
    # Minimum age required to open an account.  Enforced at registration by
    # validating ``birth_date`` in ``RegisterUserRequest``.
    minimum_account_age_years: int = _env_int("MINIMUM_ACCOUNT_AGE_YEARS", 18)

    # --- Data retention -------------------------------------------------------
    # GDPR-style minimisation knobs, surfaced on the public /privacy page.
    # ``conversation_history_ttl_seconds`` is how long an assistant's per-user
    # cached conversation stays in Redis before it is auto-deleted (a
    # conversation is an "AI log" too).  ``ai_logs_retention_days`` is the
    # retention window for cached AI interaction logs (``ai_response:*`` keys)
    # that lack an explicit expiry; the retention service applies it so old
    # logs are eventually purged.
    conversation_history_ttl_seconds: int = _env_int(
        "CONVERSATION_HISTORY_TTL_SECONDS",
        30 * 24 * 60 * 60,
    )
    ai_logs_retention_days: int = _env_int("AI_LOGS_RETENTION_DAYS", 90)

    # --- Account security -----------------------------------------------------
    # Failed-login lockout: after ``login_failed_attempts_threshold`` wrong
    # passwords within ``login_failed_attempts_window_seconds`` the account is
    # refused with a 429 until the sliding window rolls over.  A successful
    # login always resets the counter.
    login_failed_attempts_threshold: int = _env_int("LOGIN_FAILED_ATTEMPTS_THRESHOLD", 5)
    login_failed_attempts_window_seconds: int = _env_int("LOGIN_FAILED_ATTEMPTS_WINDOW_SECONDS", 900)

    # Whether the very first registrant is auto-promoted to ADMIN.  This is a
    # development convenience so a fresh local database has an administrator
    # without running the bootstrap script.  It is **force-disabled** in
    # production (see :meth:`first_registrant_becomes_admin`) so a public
    # deployment can never hand the ADMIN role to whoever signs up first — in
    # production the administrator comes exclusively from the startup
    # bootstrap (``ADMIN_BOOTSTRAP_EMAIL`` / ``ADMIN_BOOTSTRAP_PASSWORD``).
    first_user_admin: bool = _env_bool("FIRST_USER_ADMIN", True)

    @property
    def first_registrant_becomes_admin(self) -> bool:
        """True when a first registration may self-assign the ADMIN role.

        Always ``False`` when ``environment == "production"`` regardless of
        the ``FIRST_USER_ADMIN`` flag, so an operator cannot accidentally
        leave the public deployment open to privilege self-assignment.
        """
        return self.first_user_admin and self.environment != "production"

    @property
    def is_production(self) -> bool:
        """True when the application runs with ``ENVIRONMENT=production``.

        Centralises the check so every caller (bootstrap guard, cookie
        hardening, secret validation) agrees on what "production" means.
        """
        return self.environment == "production"

    @property
    def admin_allowed_ips(self) -> list[str]:
        """Parse the optional panel source-IP allowlist (CSV)."""
        raw = self.admin_allowed_ips_raw or os.getenv("ADMIN_ALLOWED_IPS")
        if not raw:
            return []
        return [item.strip() for item in str(raw).split(",") if item.strip()]

    # --- Data export ---------------------------------------------------------
    # Stricter ceiling for ``GET /api/v1/me/export``, layered on top of the
    # generic per-IP middleware bucket (export bundles the whole account, so
    # it is deliberately throttled per user, not just per client).
    data_export_max_per_hour: int = _env_int("DATA_EXPORT_MAX_PER_HOUR", 10)
    data_export_window_seconds: int = _env_int("DATA_EXPORT_WINDOW_SECONDS", 3600)

    # Pydantic-settings configuration: read from ``.env`` file, case-insensitive.
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)

    @property
    def google_play_products(self) -> dict[str, str]:
        """Map plan name → Google Play product id.

        Parsed from ``GOOGLE_PLAY_PRODUCTS_JSON``; a malformed value yields an
        empty mapping so billing degrades to "not configured" instead of
        crashing at request time.
        """
        try:
            data = json.loads(self.google_play_products_json)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

    @property
    def plan_by_product_id(self) -> dict[str, str]:
        """Inverse of :attr:`google_play_products` (product id → plan name)."""
        return {product_id: plan for plan, product_id in self.google_play_products.items()}

    @property
    def consent_versions(self) -> dict[str, str]:
        """Map consent type → current document version from ``CONSENT_VERSIONS_JSON``.

        A malformed value yields an empty mapping so consent recording degrades
        to "no version" instead of crashing at request time.
        """
        try:
            data = json.loads(self.consent_versions_json)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): str(value) for key, value in data.items()}

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

    @property
    def ai_provider_catalog(self) -> list[AIProviderConfig]:
        """Return the whole declared AI fleet, **before** any filtering.

        Unlike :attr:`ai_providers` this keeps disabled providers and ones with
        no API key, so the admin "Operações de IA" page can show a provider that
        was configured but is currently unusable (and why).  Credentials are
        still never part of what is displayed; callers only inspect
        ``bool(config.api_key)``.
        """
        raw = self.ai_providers_json
        if raw and raw.strip():
            data = json.loads(raw)
            if not isinstance(data, list):
                return []
            return [AIProviderConfig(**item) for item in data if isinstance(item, dict)]
        return [
            AIProviderConfig(
                name="groq",
                base_url=self.groq_api_base_url,
                api_key=self.groq_api_key,
                default_model=self.groq_api_model,
                priority=10,
                timeout_seconds=self.groq_api_timeout_seconds,
                rpm=self.groq_rate_limit_rpm,
                tpm=self.groq_token_budget_per_minute,
                max_concurrency=self.groq_max_concurrency,
                max_attempts=self.groq_max_attempts,
                max_output_tokens=self.groq_max_output_tokens,
            ),
            AIProviderConfig(
                name="openai",
                base_url=self.openai_api_base_url,
                api_key=self.openai_api_key,
                default_model=self.openai_api_model,
                priority=20,
            ),
            AIProviderConfig(
                name="openrouter",
                base_url=self.openrouter_api_base_url,
                api_key=self.openrouter_api_key,
                default_model=self.openrouter_api_model,
                priority=30,
            ),
        ]

    @property
    def ai_providers(self) -> list[AIProviderConfig]:
        """Return the AI provider fleet, ordered by ``priority`` (lowest first).

        When ``AI_PROVIDERS_JSON`` is set it defines the whole fleet — useful
        for operators that need provider-specific ``models`` routing or deeper
        breaker/pacer tuning.  Otherwise the well-known providers are derived
        from their own env groups and only the ones with an API key are kept,
        so a deployment that just sets ``GROQ_API_KEY`` behaves exactly as it
        did before the multi-provider refactor.

        A provider marked ``enabled=false`` or lacking an API key is always
        dropped here, which lets the router assume every entry is usable.  See
        :attr:`ai_provider_catalog` for the unfiltered list.
        """
        return sorted(
            (p for p in self.ai_provider_catalog if p.enabled and p.api_key),
            key=lambda p: p.priority,
        )


# ---------------------------------------------------------------------------
# Module-level configuration validation (runs on import)
# ---------------------------------------------------------------------------
# Warn if the default weak admin password is in use, so operators are
# aware before the application starts serving traffic.


def _validate_admin_bootstrap() -> None:
    """Warn once when the deprecated admin bootstrap variables are in use.

    ``ADMIN_DEFAULT_EMAIL`` / ``ADMIN_DEFAULT_PASSWORD`` (and the older
    ``DEFAULT_ADMIN_PASSWORD``) still work as aliases of the canonical
    ``ADMIN_BOOTSTRAP_EMAIL`` / ``ADMIN_BOOTSTRAP_PASSWORD``, but they are
    deprecated: one warning is emitted at startup so the operator can rename
    them.  Only the *names* of the variables are logged — never a password,
    never a hash.
    """
    settings = get_settings()
    deprecated = settings.admin_bootstrap_deprecated_env
    if not deprecated:
        return

    from app.core.logging import get_logger

    logger = get_logger(__name__)
    logger.warning(
        "Deprecated admin bootstrap environment variable(s) in use: %s — rename them to "
        "ADMIN_BOOTSTRAP_EMAIL / ADMIN_BOOTSTRAP_PASSWORD.",
        ", ".join(deprecated),
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
    * No AI provider is configured (no Groq/OpenAI/OpenRouter key and no
      ``AI_PROVIDERS_JSON``).
    """
    settings = get_settings()
    if settings.environment != "production":
        return
    if settings.secret_key == "change-me-in-production":
        raise RuntimeError("SECRET_KEY must be set to a strong, non-default value when ENVIRONMENT=production.")
    if len(settings.secret_key.encode("utf-8")) < 32:
        raise RuntimeError("SECRET_KEY must contain at least 32 bytes when ENVIRONMENT=production.")
    if settings.minimum_account_age_years < 18:
        raise RuntimeError("MINIMUM_ACCOUNT_AGE_YEARS cannot be lower than 18 in production.")
    if not settings.master_api_key or settings.master_api_key in _PLACEHOLDER_VALUES:
        raise RuntimeError("MASTER_API_KEY must be set to a strong, non-placeholder value when ENVIRONMENT=production.")
    if len(settings.master_api_key.encode("utf-8")) < 32:
        raise RuntimeError("MASTER_API_KEY must contain at least 32 bytes when ENVIRONMENT=production.")
    has_ai_provider = any(
        (
            settings.groq_api_key,
            settings.openai_api_key,
            settings.openrouter_api_key,
            bool(settings.ai_providers_json and settings.ai_providers_json.strip()),
        )
    )
    if not has_ai_provider:
        raise RuntimeError(
            "At least one AI provider must be configured when ENVIRONMENT=production. "
            "Set GROQ_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY or AI_PROVIDERS_JSON."
        )
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
    # The Google Play application id is required in production: the RTDN push
    # endpoint and the purchase-verification flow both key on it, and a wrong
    # or missing value silently fails every real purchase.  It has a safe
    # default (``br.com.novaiax.agente``) so the app still boots, but the value
    # MUST be a syntactically valid Android package name.
    if not settings.google_play_package_name or not re.fullmatch(
        r"[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+", settings.google_play_package_name
    ):
        raise RuntimeError(
            "GOOGLE_PLAY_PACKAGE_NAME must be a valid Android package name "
            "(e.g. br.com.novaiax.agente) when ENVIRONMENT=production."
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
        _validate_admin_bootstrap()
        _validate_master_api_key_visibility()
        _validate_production_secrets()
    return _settings
