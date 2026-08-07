"""
Repository layer for the ``User`` model.

The repository pattern abstracts database access behind a simple,
testable interface.  By isolating SQLAlchemy queries in a dedicated
class, the service layer remains free of ORM-specific code and can be
unit-tested with a mock repository.
"""

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole


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

    async def get_by_id(self, id: int, for_update: bool = False) -> User | None:
        """Return a user by ID, optionally locking the row for update."""
        stmt = select(User).where(User.id == id)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def exists_admin(self, for_update: bool = False) -> bool:
        """Return whether any admin exists in the system."""
        stmt = select(User).where(User.role == UserRole.ADMIN).limit(1)
        if for_update:
            stmt = stmt.with_for_update()
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def count_admins(self, for_update: bool = False) -> int:
        """Return the number of admin users in the system."""
        if for_update:
            stmt = select(User.id).where(User.role == UserRole.ADMIN).with_for_update()
            result = await self.session.execute(stmt)
            return len(result.scalars().all())

        stmt = select(func.count()).select_from(User).where(User.role == UserRole.ADMIN)
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def update_role(self, id: int, role: UserRole) -> User | None:
        """Update a user's role and return the updated user."""
        user = await self.get_by_id(id, for_update=True)
        if user is None:
            return None
        user.role = role
        await self.session.flush()
        await self.session.refresh(user)
        return user

    async def delete_by_id(self, id: int, for_update: bool = False) -> bool:
        """Delete a user by ID.

        If ``for_update`` is set, the target row is locked before deletion.
        Returns ``True`` when a user was found and marked for deletion,
        otherwise ``False``.
        """
        user = await self.get_by_id(id, for_update=for_update)
        if user is None:
            return False
        await self.session.delete(user)
        return True

    async def lock_users_table(self) -> None:
        """Acquire a lock on the users table to serialize role decisions."""
        await self.session.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))

    async def create(
        self,
        *,
        full_name: str,
        email: str,
        password_hash: str,
        role: UserRole | None = None,
        commit: bool = True,
    ) -> User:
        """Create and persist a new user.

        The ``*`` in the parameter list enforces keyword-only arguments,
        making call sites more explicit and less error-prone.

        Args:
            full_name: The user's display name.
            email: The user's unique email address.
            password_hash: The PBKDF2 hash of the user's password.
            role: Optional role to assign; defaults to the database default.
            commit: Whether to commit the transaction immediately.

        Returns:
            The newly created :class:`User` instance (with ``id``
            populated after ``refresh``).
        """
        user = User(full_name=full_name, email=email, password_hash=password_hash)
        if role is not None:
            user.role = role
        self.session.add(user)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        await self.session.refresh(user)
        return user
