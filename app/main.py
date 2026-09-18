"""
Application entry point for the NovaIAx API Gateway.

This module is responsible for:
   - Bootstrapping the Python path so that the `app` package can be imported
     regardless of the current working directory.
   - Creating and configuring the FastAPI application instance.
   - Registering all middleware, routers, and exception handlers.
   - Exposing a simple health-check endpoint.

The gateway sits in front of one or more downstream microservices (e.g. an
AI service) and provides cross-cutting concerns such as authentication,
rate-limiting, caching, and request logging.
"""

import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
# Ensure the project root is on ``sys.path`` so that absolute imports like
# ``from app.api.v1.router import router`` work even when the application is
# launched from a different working directory (e.g. via ``uvicorn app.main``).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
APP_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Application imports (must come *after* the path bootstrap above)
# ---------------------------------------------------------------------------
from app.api.v1.router import router as v1_router
from app.api.admin import register_admin_exception_handler, router as admin_router
from app.core.config import get_settings
from app.core.health import check_database, check_redis
from app.core.logging import configure_logging, get_logger
from app.exceptions.handlers import register_exception_handlers
from app.middlewares.auth_middleware import AuthMiddleware
from app.middlewares.logging_middleware import LoggingMiddleware
from app.middlewares.rate_limit_middleware import RateLimitMiddleware
from app.middlewares.security_headers_middleware import SecurityHeadersMiddleware

# Module-level logger used by the health-check endpoint.
logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Global setup
# ---------------------------------------------------------------------------
# Configure structured (JSON) logging as early as possible so that every
# subsequent log message — including those emitted during startup — is
# captured in the correct format.
configure_logging()

# Load application settings from environment variables / ``.env`` file.
# ``get_settings`` returns a singleton, so this is cheap to call repeatedly.
settings = get_settings()


# ---------------------------------------------------------------------------
# Lifespan handler
# ---------------------------------------------------------------------------
# The lifespan context manager replaces the deprecated ``@app.on_event``
# decorators.  It runs once when the application starts up and once when it
# shuts down.
#
# IMPORTANT: The database schema is managed exclusively by Alembic.  The
# application must NOT create or alter tables at startup.  Run migrations
# before starting the app:
#
#     alembic upgrade head          # local / Docker
#     alembic upgrade head         # Render (container entrypoint, below)
#
# Migrations always run before uvicorn starts (via ``entrypoint.sh``), so by
# the time this handler runs the schema is guaranteed to be up to date.  That
# makes this the right place to bootstrap the default ADMIN account.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle handler.

    All database schema changes are handled by Alembic migrations run
    before the application starts (via ``alembic upgrade head``).  No
    table creation happens here — this avoids accidentally running
    ``create_all()`` against a production database where the schema
    should only evolve via versioned migrations.

    Data bootstrap: a default ADMIN account is created on startup when no
    administrator exists yet (see :func:`app.db.bootstrap.ensure_default_admin`).
    This is idempotent — once an admin exists, every subsequent restart is a
    no-op.

    The bootstrap module is imported lazily (rather than at module level) to
    keep DB-side startup logic out of the import-time path.
    """
    from app.db.bootstrap import ensure_default_admin

    await ensure_default_admin()
    yield


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------
# Create the FastAPI application instance.  The ``title`` and ``version``
# are surfaced in the auto-generated OpenAPI documentation.
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Middleware registration
# ---------------------------------------------------------------------------
# Middleware execute in **reverse order** of registration (the last one
# added is the *outermost* layer).  The order below is intentional:
#
#   1. CORSMiddleware  – handles browser pre-flight (OPTIONS) requests.
#   2. SecurityHeaders – injects security-related HTTP headers.
#   3. LoggingMiddleware – records request/response metadata.
#   4. AuthMiddleware  – enforces authentication on protected routes.
#   5. RateLimitMiddleware – enforces per-client request-rate limits.
#
# Because Starlette wraps middleware in reverse, the *actual* execution
# order for an incoming request is:
#   RateLimit → Auth → Logging → SecurityHeaders → CORS → route handler
# and for the response the order is reversed again.

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,  # Origins permitted to make cross-origin requests.
    allow_credentials=True,  # Allow cookies / Authorization headers in CORS requests.
    allow_methods=["*"],  # Permit all HTTP methods (GET, POST, PUT, DELETE, …).
    allow_headers=["*"],  # Permit all request headers.
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(RateLimitMiddleware)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------
# Mount the v1 API router under the ``/api/v1`` prefix.  All route paths
# defined in ``v1_router`` are relative to this prefix.
app.include_router(v1_router, prefix="/api/v1")

# Server-rendered admin panel.  The routes define their own ``/admin``
# namespace and handle their own cookie-based authentication (see
# :mod:`app.api.admin`), so no extra prefix is applied.
app.include_router(admin_router)

# Static assets used by the admin panel templates.
app.mount("/admin/static", StaticFiles(directory=APP_DIR / "static"), name="admin_static")

# ---------------------------------------------------------------------------
# Exception handler registration
# ---------------------------------------------------------------------------
# Register custom exception handlers that return consistent JSON error
# responses and log unexpected failures.
register_exception_handlers(app)

# The admin panel's own redirect handler (see :mod:`app.api.admin`).
register_admin_exception_handler(app)


# ---------------------------------------------------------------------------
# Health-check endpoint
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> JSONResponse:
    """Combined liveness/readiness probe used by orchestrators and probes.

    Performs a real dependency check on every call:
        - Runs ``SELECT 1`` against the database (2s timeout).
        - Pings Redis via a fresh connection (2s timeout).

    Returns ``{"status": "ok", "db": "ok", "redis": "ok"}`` with HTTP 200
    when both dependencies are reachable.  If either check fails, the
    body reports the failing components as ``"error"`` and the response
    status is **503** so Render's health check marks the instance
    unhealthy.

    This endpoint is excluded from the auth middleware (public) and is
    fully exempted from rate limiting by :class:`RateLimitMiddleware`.
    """
    db_status = await check_database()
    redis_status = await check_redis(settings.redis_url)

    # Emit a distinct, easily-greppable log line on failure so degraded
    # health checks stand out from the generic request-completed lines.
    if db_status == "error" or redis_status == "error":
        logger.warning(
            "health_check_failed",
            extra={
                "db": db_status,
                "redis": redis_status,
                "path": "/health",
            },
        )

    all_ok = db_status == "ok" and redis_status == "ok"
    status = "ok" if all_ok else "error"
    status_code = 200 if all_ok else 503

    return JSONResponse(
        status_code=status_code,
        content={"status": status, "db": db_status, "redis": redis_status},
    )
