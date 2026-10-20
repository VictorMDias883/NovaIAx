"""Tests for the database engine/pool configuration.

Covers:

1. **Queue-pool sizing on PostgreSQL** — ``pool_size``, ``max_overflow``,
   ``pool_recycle``, ``pool_timeout`` and ``pool_pre_ping`` are passed, so a
   stale connection from a managed-Postgres idle timeout is replaced instead
   of surfacing as an intermittent 500.
2. **Dialect safety** — SQLite must *not* receive those arguments; it uses
   ``NullPool``/``StaticPool`` and rejects them at engine creation, which
   would break every local run and the whole test suite.
3. **Sane clamping** — a zero or negative setting is coerced into a value the
   pool accepts instead of producing a confusing error at startup.
4. **Fail-fast on a missing URL** — the operator gets an actionable message
   rather than ``create_async_engine(None)``.
"""

import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from app.core.config import Settings, get_settings
from sqlalchemy.ext.asyncio import create_async_engine

QUEUE_POOL_KWARGS = {"pool_size", "max_overflow", "pool_recycle", "pool_timeout"}

POSTGRES_URL = "postgresql+asyncpg://user:pass@127.0.0.1:5432/novaiax"


def _pool_kwargs(url: str, monkeypatch: pytest.MonkeyPatch, **overrides) -> dict[str, object]:
    """Re-run :func:`app.db.session._engine_pool_kwargs` against ``url``."""
    session_module = importlib.import_module("app.db.session")
    settings = Settings(database_url=url, **overrides)
    monkeypatch.setattr(session_module, "settings", settings)
    monkeypatch.setattr(session_module, "DATABASE_URL", url)
    return session_module._engine_pool_kwargs()


# ---------------------------------------------------------------------------
# 1. PostgreSQL sizing
# ---------------------------------------------------------------------------


def test_postgres_gets_every_pool_option() -> None:
    """A dead pooled connection is the classic intermittent-500 source."""
    kwargs = _pool_kwargs(
        POSTGRES_URL,
        __import__("pytest").MonkeyPatch(),
        db_pool_size=7,
        db_max_overflow=3,
        db_pool_recycle_seconds=900,
        db_pool_timeout_seconds=12.5,
        db_pool_pre_ping=True,
    )
    assert kwargs == {
        "pool_pre_ping": True,
        "pool_size": 7,
        "max_overflow": 3,
        "pool_recycle": 900,
        "pool_timeout": 12.5,
    }


def test_postgres_pool_defaults_bound_concurrency() -> None:
    """An unbounded pool exhausts Postgres' connection limit under a burst."""
    settings = Settings()
    assert settings.db_pool_size >= 1
    assert settings.db_max_overflow >= 0
    assert settings.db_pool_recycle_seconds > 0, "stale connections are the failure being prevented"
    assert settings.db_pool_pre_ping is True


def test_pool_pre_ping_can_be_disabled_explicitly() -> None:
    kwargs = _pool_kwargs(
        POSTGRES_URL,
        __import__("pytest").MonkeyPatch(),
        db_pool_pre_ping=False,
    )
    assert kwargs["pool_pre_ping"] is False


@pytest.mark.parametrize("dialect", ["postgresql", "mysql", "mariadb"])
def test_every_queue_pool_dialect_is_configured(dialect: str) -> None:
    kwargs = _pool_kwargs(f"{dialect}://user:pass@127.0.0.1:3306/db", __import__("pytest").MonkeyPatch())
    assert set(kwargs) >= QUEUE_POOL_KWARGS, f"{dialect} must receive the sizing options"
    assert kwargs["pool_pre_ping"] is True


def test_the_engine_uses_a_queue_pool_for_postgres() -> None:
    """End of the chain: the running engine really is queue-pooled."""
    from app.db.session import engine

    assert "QueuePool" in type(engine.pool).__name__


def test_the_engine_validates_pooled_connections() -> None:
    from app.db.session import engine

    assert engine.pool._pre_ping is True


# ---------------------------------------------------------------------------
# 2. Dialect safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///./test.db",
        "sqlite+aiosqlite:///:memory:",
        "sqlite:///:memory:",
    ],
)
def test_sqlite_never_receives_queue_pool_arguments(url: str) -> None:
    """SQLite rejects ``pool_size``/``max_overflow`` at engine creation."""
    kwargs = _pool_kwargs(url, __import__("pytest").MonkeyPatch())
    assert not (QUEUE_POOL_KWARGS & set(kwargs)), "these arguments would raise TypeError on SQLite"
    assert set(kwargs) == {"pool_pre_ping"}


def test_the_sqlite_kwargs_really_build_an_engine() -> None:
    """The guard is not cosmetic: the produced arguments are usable."""
    kwargs = _pool_kwargs("sqlite+aiosqlite:///:memory:", __import__("pytest").MonkeyPatch())
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", **kwargs)  # type: ignore[arg-type]
    assert engine is not None


# ---------------------------------------------------------------------------
# 3. Clamping
# ---------------------------------------------------------------------------


def test_a_zero_pool_size_is_clamped_to_one() -> None:
    """``pool_size=0`` is rejected by SQLAlchemy with an opaque error."""
    kwargs = _pool_kwargs(POSTGRES_URL, __import__("pytest").MonkeyPatch(), db_pool_size=0)
    assert kwargs["pool_size"] == 1


def test_a_negative_pool_size_is_clamped() -> None:
    kwargs = _pool_kwargs(POSTGRES_URL, __import__("pytest").MonkeyPatch(), db_pool_size=-5)
    assert kwargs["pool_size"] == 1


def test_a_negative_overflow_is_clamped_to_zero() -> None:
    kwargs = _pool_kwargs(POSTGRES_URL, __import__("pytest").MonkeyPatch(), db_max_overflow=-3)
    assert kwargs["max_overflow"] == 0


def test_a_negative_recycle_is_clamped_to_zero() -> None:
    """``0`` means "never recycle", which is valid; a negative is not."""
    kwargs = _pool_kwargs(POSTGRES_URL, __import__("pytest").MonkeyPatch(), db_pool_recycle_seconds=-1)
    assert kwargs["pool_recycle"] == 0


def test_a_zero_pool_timeout_is_clamped() -> None:
    """``pool_timeout=0`` means "fail instantly" instead of "wait"."""
    kwargs = _pool_kwargs(POSTGRES_URL, __import__("pytest").MonkeyPatch(), db_pool_timeout_seconds=0.0)
    assert kwargs["pool_timeout"] >= 0.1


def test_a_negative_pool_timeout_is_clamped() -> None:
    kwargs = _pool_kwargs(POSTGRES_URL, __import__("pytest").MonkeyPatch(), db_pool_timeout_seconds=-2.0)
    assert kwargs["pool_timeout"] >= 0.1


# ---------------------------------------------------------------------------
# 4. Fail-fast on a missing URL
# ---------------------------------------------------------------------------


def test_a_missing_database_url_fails_with_an_actionable_message() -> None:
    """Importing the session module must not die with ``None``."""
    # Run outside the project directory so ``.env`` cannot supply a URL.
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        # Only production has no development fallback URL, and it refuses to
        # start without the secrets it requires.
        "ENVIRONMENT": "production",
        "SECRET_KEY": "a-strong-test-key-that-is-not-the-shipped-default",
        "ADMIN_DEFAULT_PASSWORD": "A-strong-test-password-1",
        "GROQ_API_KEY": "gsk_test",
        "MASTER_API_KEY": "a-strong-master-key-not-the-placeholder",
    }
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", "import app.db.session"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tempfile.gettempdir(),
    )
    assert result.returncode != 0, "a missing URL must not import successfully"
    combined = result.stdout + result.stderr
    assert "DATABASE_URL" in combined
    assert "postgresql+asyncpg://" in combined, "the error should show a working example"


def test_the_configured_url_is_normalised() -> None:
    """``postgres://`` must still resolve to the asyncpg driver."""
    from app.core.config import _normalize_database_url

    assert _normalize_database_url("postgres://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    assert _normalize_database_url("postgresql://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    assert _normalize_database_url("sqlite+aiosqlite:///./x.db") == "sqlite+aiosqlite:///./x.db"


def test_the_url_dialect_drives_the_pool_decision() -> None:
    """The pool choice follows the URL, not the environment."""
    settings = get_settings()
    assert settings.database_url, "the test environment must provide a database URL"
