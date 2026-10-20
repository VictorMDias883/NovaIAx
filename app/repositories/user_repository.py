"""
Repository layer for the ``User`` model.

The repository pattern abstracts database access behind a simple,
testable interface.  By isolating SQLAlchemy queries in a dedicated
class, the service layer remains free of ORM-specific code and can be
unit-tested with a mock repository.
"""

import contextlib
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

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

    async def search(
        self, query: str | None = None, *, page: int = 1, limit: int = 25
    ) -> tuple[list[User], int]:
        """Return one page of users matching ``query`` by e-mail, name or id.

        ``query`` is optional: when empty the whole table is listed in id
        order.  A numeric query also matches the exact user id, which is
        handy for support looking a user up by what they see in a screenshot.
        """
        clauses: list[Any] = []
        clean = (query or "").strip()
        if clean:
            pattern = f"%{clean}%"
            conditions: list[ColumnElement[bool]] = [User.email.ilike(pattern), User.full_name.ilike(pattern)]
            with contextlib.suppress(ValueError):
                conditions.append(User.id == int(clean))
            clauses.append(or_(*conditions))

        page = max(1, page)
        limit = min(max(1, limit), 100)
        stmt = (
            select(User)
            .where(*clauses)
            .order_by(User.id)
            .offset((page - 1) * limit)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        count_stmt = select(func.count()).select_from(User)
        for clause in clauses:
            count_stmt = count_stmt.where(clause)
        total = int((await self.session.execute(count_stmt)).scalar_one())
        return rows, total

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

    async def update_password_hash(self, id: int, password_hash: str) -> User | None:
        """Replace a user's password hash and return the updated user.

        The row is locked for update so concurrent writes cannot be lost.
        Returns ``None`` when no user has the given ``id``.
        """
        user = await self.get_by_id(id, for_update=True)
        if user is None:
            return None
        user.password_hash = password_hash
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
        """Acquire a lock on the users table to serialize role decisions (PostgreSQL only)."""
        bind = self.session.get_bind()
        if bind and bind.dialect.name == "postgresql":
            await self.session.execute(text("LOCK TABLE users IN SHARE ROW EXCLUSIVE MODE"))

    async def create(
        self,
        *,
        full_name: str,
        email: str,
        password_hash: str,
        role: UserRole | None = None,
        birth_date: date | None = None,
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
            birth_date: Optional ``datetime.date`` for the minimum-age gate;
                stored on the account for future verification.
            commit: Whether to commit the transaction immediately.

        Returns:
            The newly created :class:`User` instance (with ``id``
            populated after ``refresh``).
        """
        # ``refresh_token_valid_after`` starts at "now" so no refresh token is
        # invalidated out of the box, but a JWT ``iat`` is a whole second while
        # ``now()`` carries microseconds.  Storing the sub-second part would
        # reject the very first token the account receives (the one issued
        # moments later in the same second), so the initial value is truncated
        # to the second.  Later password changes write full-precision values,
        # which is what invalidates previously issued tokens.
        user = User(
            full_name=full_name,
            email=email,
            password_hash=password_hash,
            refresh_token_valid_after=datetime.fromtimestamp(int(datetime.now(UTC).timestamp()), tz=UTC),
        )
        if role is not None:
            user.role = role
        if birth_date is not None:
            user.birth_date = birth_date
        self.session.add(user)
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        await self.session.refresh(user)
        return user
