"""
Database session and engine configuration.

This module sets up the SQLAlchemy asynchronous engine and session
factory used throughout the application.  It also defines the
declarative base class that all ORM models inherit from.

The application defaults to PostgreSQL via the ``DATABASE_URL``
environment variable, matching the Docker Compose stack.
"""

import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://novaiax:novaiax@192.168.15.4:5432/novaiax",
)

# Create the async engine.  ``echo=False`` disables SQL query logging;
# set to ``True`` for debugging.
engine = create_async_engine(DATABASE_URL, echo=False)

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

    This function should be called once at application startup (or
    before running tests) to ensure the database schema exists.
    It uses ``Base.metadata.create_all`` which is idempotent — it
    only creates tables that do not already exist.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
