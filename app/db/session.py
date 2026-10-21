"""
Database session and engine configuration.

This module sets up the SQLAlchemy asynchronous engine and session
factory used throughout the application.  It also defines the
declarative base class that all ORM models inherit from.

The connection URL comes from the :class:`Settings` singleton
(``app.core.config``), which reads the ``DATABASE_URL`` environment
variable.  The application defaults to PostgreSQL, matching the
Docker Compose stack.
"""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

# The engine needs a concrete URL.  ``database_url`` is ``str | None`` in
# the settings model, so fail with a clear message instead of letting
# ``create_async_engine`` receive ``None``.
_database_url = settings.database_url
if not _database_url:
    raise RuntimeError(
        "DATABASE_URL must be set before the database engine can be created. "
        "Use a PostgreSQL connection string, e.g. "
        "postgresql+asyncpg://user:pass@<database-host>:5432/dbname"
    )
#: The validated, non-optional connection string.
DATABASE_URL: str = _database_url


def _engine_pool_kwargs() -> dict[str, object]:
    """Build the engine keyword arguments that the configured dialect accepts.

    Queue-based pools (``postgresql``, ``mysql``, ``mariadb``) take
    ``pool_size``/``max_overflow``/``pool_recycle``; SQLite's ``aiosqlite``
    dialect does not — it uses ``NullPool`` for file databases and
    ``StaticPool`` for in-memory ones — and passing those arguments raises
    ``TypeError`` at engine creation, which would break every local run and
    the whole test suite.  ``pool_pre_ping`` is supported by both, so it is
    always applied.
    """
    kwargs: dict[str, object] = {"pool_pre_ping": settings.db_pool_pre_ping}
    dialect = make_url(DATABASE_URL).get_backend_name()
    if dialect in _QUEUE_POOL_DIALECTS:
        kwargs.update(
            pool_size=max(settings.db_pool_size, 1),
            max_overflow=max(settings.db_max_overflow, 0),
            pool_recycle=max(settings.db_pool_recycle_seconds, 0),
            pool_timeout=max(settings.db_pool_timeout_seconds, 0.1),
        )
    return kwargs


#: Backends whose async drivers use a queue-based pool with sizing knobs.
_QUEUE_POOL_DIALECTS = frozenset({"postgresql", "mysql", "mariadb"})


def _connect_args() -> dict[str, object]:
    """Return driver-specific connect arguments for the configured URL.

    Supabase's **transaction pooler** (port ``6543``, PgBouncer) multiplexes
    many client connections onto few server ones, so a server-side prepared
    statement cached on one transaction may be absent on the next.  asyncpg's
    statement cache must therefore be disabled, otherwise queries fail with
    ``prepared statement ... does not exist``.  The **session pooler**
    (port ``5432``) and a direct connection do not need this.

    Alembic is run against the direct/session connection (see ``DEPLOY.md``).
    """
    url = make_url(DATABASE_URL)
    if url.get_backend_name() == "postgresql" and url.port == 6543:
        return {"statement_cache_size": 0}
    return {}

# Create the async engine.  ``echo=False`` disables SQL query logging;
# set to ``True`` for debugging.
#
# Pool options matter on any long-running deployment (Render, Docker, a
# laptop left switched on all day).  Managed Postgres and most proxies close
# idle connections after a few minutes, so a connection handed out from the
# pool can already be dead by the time a query runs on it — which surfaces
# to the user as an intermittent 500.  Two settings remove that class of
# failure entirely:
#
#   * ``pool_pre_ping=True`` validates a connection with a cheap ``SELECT 1``
#     before handing it out and transparently replaces it if it is dead;
#   * ``pool_recycle`` proactively retires connections before the provider
#     would drop them in the first place.
#
# ``pool_size``/``max_overflow`` bound concurrency (10 + 10 by default) so a
# burst of requests queues for a connection instead of exhausting Postgres'
# connection limit and taking the whole instance down.
#
# ``hide_parameters=True`` keeps the bound values out of the SQLAlchemy
# exception messages: a failed insert would otherwise expose the bound
# parameters (a password **hash** among them) in the logs, since the error
# text quotes the statement and its parameters.
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    connect_args=_connect_args(),
    hide_parameters=True,
    **_engine_pool_kwargs(),
)

# Session factory: each call to ``SessionLocal()`` returns a new
# ``AsyncSession`` instance.  ``expire_on_commit=False`` prevents
# SQLAlchemy from expiring (lazily reloading) model attributes after
# a commit, which avoids extra queries in async contexts.
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Declarative base class for all ORM models.

    All model classes (e.g. :class:`User`, :class:`Objective`) inherit
    from this class, which provides the ``__table__`` metadata and
    mapping configuration.
    """
    pass


async def init_db() -> None:
    """Create all database tables defined by models that inherit from :class:`Base`.

    .. warning::

        This function is intended **exclusively for the test suite**
        (and local dev) where running ``alembic upgrade head`` against a
        throwaway SQLite database is impractical.  Production and
        Render deployments must use ``alembic upgrade head`` via the
        container entrypoint instead.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
