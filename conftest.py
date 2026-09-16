"""
Pytest configuration and path bootstrap.

This file ensures that the project root directory is on ``sys.path``
so that test modules can import the ``app`` package using absolute
imports (e.g. ``from app.main import app``).

It also defaults ``DATABASE_URL`` to a local SQLite file when the
variable is not set explicitly, so the full middleware/router stack can
be exercised in integration tests without requiring PostgreSQL.  This
assignment happens at conftest import time — before any test module is
imported — so the engine created by :mod:`app.db.session` picks it up.

A session-scoped autouse fixture ensures all tables exist for the
test database before any test runs.  This replaces the old
``init_db()`` call that was removed from the application lifespan
in favour of Alembic migrations (``alembic upgrade head``) in
production.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Integration tests (e.g. ``tests/test_auth_and_rate_limit.py``) import
# ``app.main`` directly; give them a working database by default.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")

# ---------------------------------------------------------------------------
# Session-scoped fixture: create all tables once before the first test.
# ---------------------------------------------------------------------------
# ``app.db.session`` builds its engine from ``DATABASE_URL`` at import
# time, so it must be imported only *after* the env var is set above.
import pytest  # noqa: E402
from app.db.session import init_db as _create_all_tables  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_database() -> None:
    """Run ``Base.metadata.create_all`` against the test database.

    Uses the same SQLite engine configured via the ``DATABASE_URL``
    environment variable set above.  This is idempotent — it only
    creates tables that do not already exist.
    """
    import asyncio
    asyncio.run(_create_all_tables())


@pytest.fixture(autouse=True)
def _reset_redis_fallback() -> None:
    """Reset the in-memory Redis fallback between every test.

    Tests that exercise the rate limiter (e.g. ``test_rate_limit_returns_429``)
    deliberately fill the sliding window.  Without a reset, that state would
    leak into subsequent tests sharing the same instance, throttling them
    with unexpected 429 responses.  A real Redis server used in CI/prod is
    unaffected — only the in-memory fallback store is cleared.
    """
    from app.cache.redis_client import RedisClient

    RedisClient.reset_all_memory_stores()
    yield
    RedisClient.reset_all_memory_stores()
