"""
Repository layer for the agent catalog (``agent_groups`` / ``sub_agents``).

The catalog is seeded read-mostly data: the personalization service reads the
seedable groups/sub-agents to build the ``GET`` responses, validates the
user's selected slugs and resolves them into prompt fragments.
"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_group import AgentGroup
from app.models.sub_agent import SubAgent


class AgentCatalogRepository:
    """Data-access layer for the agent catalog entities.

    Args:
        session: An open SQLAlchemy :class:`AsyncSession`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_groups(self, active_only: bool = True) -> list[AgentGroup]:
        """Return the catalog groups, ordered by ``sort_order``."""
        stmt = select(AgentGroup).order_by(AgentGroup.sort_order, AgentGroup.id)
        if active_only:
            stmt = stmt.where(AgentGroup.active.is_(True))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_group(self, slug: str) -> AgentGroup | None:
        """Return one group by slug."""
        result = await self.session.execute(select(AgentGroup).where(AgentGroup.slug == slug))
        return result.scalar_one_or_none()

    async def get_active_group(self, slug: str) -> AgentGroup | None:
        """Return one active group by slug."""
        result = await self.session.execute(
            select(AgentGroup).where(AgentGroup.slug == slug, AgentGroup.active.is_(True))
        )
        return result.scalar_one_or_none()

    async def groups_by_slugs(self, slugs: list[str]) -> list[AgentGroup]:
        """Return every group whose slug is in ``slugs`` (catalog order)."""
        if not slugs:
            return []
        result = await self.session.execute(select(AgentGroup).where(AgentGroup.slug.in_(slugs)))
        return list(result.scalars().all())

    async def groups_by_ids(self, ids: list[int]) -> dict[int, AgentGroup]:
        """Map group primary keys to their :class:`AgentGroup` instances."""
        if not ids:
            return {}
        result = await self.session.execute(select(AgentGroup).where(AgentGroup.id.in_(ids)))
        return {group.id: group for group in result.scalars().all()}

    async def list_sub_agents(self, active_only: bool = True) -> list[SubAgent]:
        """Return the sub-agents in catalog order, optionally active only."""
        stmt = select(SubAgent).order_by(SubAgent.sort_order, SubAgent.id)
        if active_only:
            stmt = stmt.where(SubAgent.active.is_(True))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_sub_agent(self, slug: str) -> SubAgent | None:
        """Return one sub-agent by slug."""
        result = await self.session.execute(select(SubAgent).where(SubAgent.slug == slug))
        return result.scalar_one_or_none()

    async def get_sub_agent_by_id(self, sub_agent_id: int) -> SubAgent | None:
        """Return one sub-agent by primary key."""
        result = await self.session.execute(select(SubAgent).where(SubAgent.id == sub_agent_id))
        return result.scalar_one_or_none()

    async def create_sub_agent(self, **values: object) -> SubAgent:
        """Insert a sub-agent and return the refreshed row."""
        sub_agent = SubAgent(**values)
        self.session.add(sub_agent)
        await self.session.commit()
        await self.session.refresh(sub_agent)
        return sub_agent

    async def update_sub_agent(self, sub_agent_id: int, **values: object) -> SubAgent | None:
        """Patch a sub-agent's columns (no-op for unknown ids)."""
        await self.session.execute(
            update(SubAgent)
            .where(SubAgent.id == sub_agent_id)
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        await self.session.commit()
        return await self.get_sub_agent_by_id(sub_agent_id)

    async def get_group_by_id(self, group_id: int) -> AgentGroup | None:
        """Return one group by primary key."""
        result = await self.session.execute(select(AgentGroup).where(AgentGroup.id == group_id))
        return result.scalar_one_or_none()

    async def create_group(self, **values: object) -> AgentGroup:
        """Insert an audience group and return the refreshed row."""
        group = AgentGroup(**values)
        self.session.add(group)
        await self.session.commit()
        await self.session.refresh(group)
        return group

    async def update_group(self, group_id: int, **values: object) -> AgentGroup | None:
        """Patch an audience group's columns (no-op for unknown ids)."""
        await self.session.execute(
            update(AgentGroup)
            .where(AgentGroup.id == group_id)
            .values(**values)
            .execution_options(synchronize_session="fetch")
        )
        await self.session.commit()
        return await self.get_group_by_id(group_id)

    async def sub_agents_by_slugs(self, slugs: list[str]) -> list[SubAgent]:
        """Return every sub-agent whose slug is in ``slugs`` (name order)."""
        if not slugs:
            return []
        result = await self.session.execute(select(SubAgent).where(SubAgent.slug.in_(slugs)))
        return list(result.scalars().all())
