"""
Repository layer for the ``UserAgent`` (personalization profile).

One profile per user.  The row is created lazily with defaults on first touch
(any personalization endpoint or personalized conversation), so existing users
start with the documented defaults without a backfill migration.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_agent import UserAgent


class UserAgentRepository:
    """Data-access layer for :class:`UserAgent` entities."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_user(self, user_id: int) -> UserAgent | None:
        """Return the user's profile, or ``None`` when none exists yet."""
        result = await self.session.execute(select(UserAgent).where(UserAgent.user_id == user_id))
        return result.scalar_one_or_none()

    async def get_or_create(self, user_id: int) -> tuple[UserAgent, bool]:
        """Return ``(profile, created)``, creating the default row on first use."""
        profile = await self.get_by_user(user_id)
        if profile is not None:
            return profile, False
        profile = UserAgent(user_id=user_id)
        self.session.add(profile)
        await self.session.flush()
        await self.session.refresh(profile)
        return profile, True

    async def update(self, profile: UserAgent) -> UserAgent:
        """Persist pending changes on a profile and return it refreshed."""
        await self.session.flush()
        await self.session.refresh(profile)
        return profile
