"""
Dependency health checks for the ``/health`` endpoint.

This module provides lightweight, timeout-bounded checks that verify
the two critical infrastructure dependencies:

* **Database** — executes ``SELECT 1`` against the connected database.
* **Redis** — pings the Redis server via a fresh connection.

Each check returns ``"ok"`` on success and ``"error"`` on failure
(timeout, connection refused, or driver error).  The endpoint uses
these results to decide whether to return HTTP 200 or 503.
"""

import asyncio
import importlib

HEALTH_CHECK_TIMEOUT_SECONDS: float = 2.0


async def check_database() -> str:
    """Run ``SELECT 1`` against the configured database.

    Uses the global :class:`Engine` from :mod:`app.db.session` so the
    connection pool is shared with the rest of the application.  The
    query is bounded by a short timeout to avoid holding up the health
    endpoint when the database is slow or unreachable.

    Returns ``"ok"`` on success, ``"error"`` otherwise.
    """
    try:
        from sqlalchemy import text

        from app.db.session import engine

        async with engine.connect() as conn:
            await asyncio.wait_for(
                conn.execute(text("SELECT 1")),
                timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
            )
        return "ok"
    except Exception:
        return "error"


async def check_redis(url: str) -> str:
    """Ping the Redis server via a **fresh** connection.

    This deliberately does *not* go through :class:`RedisClient`, whose
    ``get_client()`` method caches the result and silently falls back to
    an in-memory store.  A fresh connection ensures we detect actual
    Redis downtime regardless of the application-level fallback.

    Returns ``"ok"`` on success, ``"error"`` otherwise.
    """
    try:
        redis_asyncio = importlib.import_module("redis.asyncio")
    except ImportError:
        # The ``redis`` package is not installed — no Redis server
        # can possibly be available.
        return "error"

    client = redis_asyncio.from_url(url, decode_responses=True)
    try:
        await asyncio.wait_for(
            client.ping(),
            timeout=HEALTH_CHECK_TIMEOUT_SECONDS,
        )
        return "ok"
    except Exception:
        return "error"
    finally:
        await client.aclose()
