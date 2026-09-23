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

# Create the async engine.  ``echo=False`` disables SQL query logging;
# set to ``True`` for debugging.
engine = create_async_engine(_database_url, echo=False)

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
