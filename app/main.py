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

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
# Ensure the project root is on ``sys.path`` so that absolute imports like
# ``from app.api.v1.router import router`` work even when the application is
# launched from a different working directory (e.g. via ``uvicorn app.main``).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Application imports (must come *after* the path bootstrap above)
# ---------------------------------------------------------------------------
from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import init_db
from app.exceptions.handlers import register_exception_handlers
from app.middlewares.auth_middleware import AuthMiddleware
from app.middlewares.logging_middleware import LoggingMiddleware
from app.middlewares.rate_limit_middleware import RateLimitMiddleware
from app.middlewares.security_headers_middleware import SecurityHeadersMiddleware

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
# shuts down.  On startup we create all database tables that are defined by
# models inheriting from ``Base`` (e.g. ``User``, ``Objective``).
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifecycle: create database tables on startup.

    Calls :func:`init_db` which uses ``Base.metadata.create_all`` to
    create all tables defined by ORM models.  This is idempotent — it
    only creates tables that do not already exist.
    """
    await init_db()
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
    allow_credentials=True,                   # Allow cookies / Authorization headers in CORS requests.
    allow_methods=["*"],                      # Permit all HTTP methods (GET, POST, PUT, DELETE, …).
    allow_headers=["*"],                      # Permit all request headers.
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

# ---------------------------------------------------------------------------
# Exception handler registration
# ---------------------------------------------------------------------------
# Register custom exception handlers that return consistent JSON error
# responses and log unexpected failures.
register_exception_handlers(app)


# ---------------------------------------------------------------------------
# Health-check endpoint
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    """Simple liveness probe used by orchestrators (e.g. Kubernetes, Docker).

    Returns a 200 OK with a JSON body ``{"status": "ok"}`` when the
    application is running and able to accept requests.
    """
    return {"status": "ok"}
