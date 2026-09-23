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
    ``ADMIN_DEFAULT_PASSWORD`` (see :class:`app.core.config.Settings`; the
    legacy ``DEFAULT_ADMIN_PASSWORD`` spelling is also accepted):

    * Email falls back to ``admin@admin.com`` when unset.
    * When no password is configured, a strong random one is generated and
      logged exactly once — the first (and only) startup that creates the
      account.  The check makes subsequent restarts no-ops, so the password
      is never logged again.

    When an ADMIN with the configured email already exists (e.g. created by
    an earlier bootstrap that auto-generated a password), the startup
    **re-syncs** the stored hash to match the configured password so login
    works with the ``.env`` credentials.  Other admin accounts are never
    modified, and nothing is reset while ``ADMIN_DEFAULT_PASSWORD`` is unset.

    The plain-text password is never persisted: it is hashed with the same
    PBKDF2 context used for regular user passwords before saving, and is only
    logged when it was randomly generated.

    The check-and-create runs inside a single transaction with the users
    table locked (PostgreSQL), so concurrent cold-starts cannot create
    duplicate default admins.
    """
    settings = get_settings()
    configured_password = settings.admin_default_password
    generated_password: str | None = None

    async with SessionLocal() as session:
        repo = UserRepository(session)
        async with session.begin():
            await repo.lock_users_table()
            existing = await repo.get_by_email(settings.admin_default_email)

            # The configured admin account already exists.  If a password is
            # configured and the stored hash no longer matches it (typical when
            # an earlier bootstrap auto-generated one), re-sync it so the .env
            # credentials work.  Other admins are left untouched.
            if existing is not None:
                if (
                    existing.role == UserRole.ADMIN
                    and configured_password
                    and not _hash_matches(existing.password_hash, configured_password)
                ):
                    await repo.update_password_hash(existing.id, pwd_context.hash(configured_password))
                    logger.warning(
                        "Default ADMIN password reset to match the configured ADMIN_DEFAULT_PASSWORD",
                        extra={"email": settings.admin_default_email},
                    )
                return

            # No user holds the default email.  Only create the account when no
            # administrator exists at all (keeps the "first admin wins" rule).
            if await repo.exists_admin():
                return

            password = configured_password or secrets.token_urlsafe(32)
            if not configured_password:
                generated_password = password
            await repo.create(
                full_name="Administrator",
                email=settings.admin_default_email,
                password_hash=pwd_context.hash(password),
                role=UserRole.ADMIN,
                commit=False,
            )

    if generated_password:
        logger.info(
            "Default ADMIN account created with a generated password",
            extra={
                "email": settings.admin_default_email,
                "generated_password": generated_password,
            },
        )


def _hash_matches(password_hash: str, password: str) -> bool:
    """Return whether ``password`` verifies against ``password_hash``.

    A malformed/unknown hash never crashes the bootstrap — it is treated as a
    mismatch so the operator-configured password can take over.
    """
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False
