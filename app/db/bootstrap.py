"""
Startup bootstrap routines.

Contains the idempotent routines that run once when the application boots
— currently the creation of the default ADMIN account on first startup.

The container entrypoint (``entrypoint.sh``) runs ``alembic upgrade head``
before starting uvicorn, so the ``users`` table is guaranteed to exist by
the time :func:`ensure_default_admin` is called from the lifespan handler.
"""

import secrets

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import pwd_context
from app.db.session import SessionLocal
from app.models.user import UserRole
from app.repositories.user_repository import UserRepository

logger = get_logger(__name__)


async def ensure_default_admin() -> None:
    """Create the default ADMIN account if no administrator exists yet.

    Reads the target credentials from ``ADMIN_DEFAULT_EMAIL`` and
    ``ADMIN_DEFAULT_PASSWORD`` (see :class:`app.core.config.Settings`):

    * Email falls back to ``admin@admin.com`` when unset.
    * When no password is configured, a strong random one is generated and
      logged exactly once — the first (and only) startup that creates the
      account.  The check makes subsequent restarts no-ops, so the password
      is never logged again.

    The plain-text password is never persisted: it is hashed with the same
    PBKDF2 context used for regular user passwords before saving, and is only
    logged when it was randomly generated.

    The check-and-create runs inside a single transaction with the users
    table locked (PostgreSQL), so concurrent cold-starts cannot create
    duplicate default admins.
    """
    settings = get_settings()

    async with SessionLocal() as session:
        repo = UserRepository(session)
        async with session.begin():
            await repo.lock_users_table()
            if await repo.exists_admin():
                return

            password = settings.admin_default_password or secrets.token_urlsafe(32)
            password_hash = pwd_context.hash(password)
            await repo.create(
                full_name="Administrator",
                email=settings.admin_default_email,
                password_hash=password_hash,
                role=UserRole.ADMIN,
                commit=False,
            )

    if not settings.admin_default_password:
        logger.info(
            "Default ADMIN account created with a generated password",
            extra={
                "email": settings.admin_default_email,
                "generated_password": password,
            },
        )
