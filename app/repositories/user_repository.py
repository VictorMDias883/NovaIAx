"""
Repository layer for the ``User`` model.

The repository pattern abstracts database access behind a simple,
testable interface.  By isolating SQLAlchemy queries in a dedicated
class, the service layer remains free of ORM-specific code and can be
unit-tested with a mock repository.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class UserRepository:
    """Data-access layer for :class:`User` entities.

    Each method receives an :class:`AsyncSession` (injected via the
    constructor) and performs a single database operation.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Store the async database session for use in queries.

        Args:
            session: An open SQLAlchemy :class:`AsyncSession`.
        """
        self.session = session

    async def get_by_email(self, email: str) -> User | None:
        """Look up a user by their email address.

        Uses ``scalar_one_or_none()`` which returns the single matching
        row, or ``None`` if no user has that email.  If multiple users
        share the same email (should not happen due to the unique
        constraint), a ``MultipleResultsFound`` error is raised.

        Args:
            email: The email address to search for.

        Returns:
            The matching :class:`User` instance, or ``None``.
        """
        result = await self.session.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def create(self, *, full_name: str, email: str, password_hash: str) -> User:
        """Create and persist a new user.

        The ``*`` in the parameter list enforces keyword-only arguments,
        making call sites more explicit and less error-prone.

        Args:
            full_name: The user's display name.
            email: The user's unique email address.
            password_hash: The PBKDF2 hash of the user's password.

        Returns:
            The newly created :class:`User` instance (with ``id``
            populated after ``commit`` and ``refresh``).
        """
        user = User(full_name=full_name, email=email, password_hash=password_hash)
        self.session.add(user)
        await self.session.commit()
        # ``refresh`` reloads the instance from the database, ensuring
        # that server-generated values (e.g. ``id``, ``created_at``)
        # are available on the returned object.
        await self.session.refresh(user)
        return user
