"""
Service layer for the agent personalization domain.

Coordinates the catalog (``GET /agent-groups``, ``GET /sub-agents``), the
per-user profile (``GET/PUT /me/agent``), the memory CRUD
(``GET/POST/PATCH/DELETE /me/memories``) and the learning toggle
(``PUT /me/learning``).

Validation that lives here (as opposed to the schema layer):

* the agent name — its length was already checked by the schema; the filter
  here rejects offensive/impersonation names (error surfaced as 422);
* selected groups must be **active** groups;
* selected sub-agents must exist **and** satisfy the sub-agent's minimum plan
  tier (every seeded sub-agent is ``free``, so all current users qualify);
* memory keys created by the user may not use the reserved ``auto_`` prefix,
  which is owned by the auto-learning pipeline.

All mutating methods own their write transaction (``session.begin()``); the
request-scoped session arrives with no open transaction because the auth
dependency releases its read transaction.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.plan_limits import FEATURE_GROUPS, FEATURE_SUB_AGENTS, PLAN_RANK
from app.commands.agent_memory_commands import CreateUserMemoryCommand, UpdateUserMemoryCommand
from app.commands.update_agent_profile_command import UpdateAgentProfileCommand
from app.models.agent_group import AgentGroup
from app.models.sub_agent import SubAgent
from app.models.user_agent import UserAgent
from app.models.user_memory import UserMemory
from app.repositories.agent_catalog_repository import AgentCatalogRepository
from app.repositories.user_agent_repository import UserAgentRepository
from app.repositories.user_memory_repository import UserMemoryRepository
from app.services.subscription_service import SubscriptionService, enforce_count_limit, enforce_feature
from app.utils.name_filter import blocked_name_reason

#: Reserved key prefix owned by the auto-learning pipeline.
RESERVED_MEMORY_PREFIX = "auto_"


class AgentPersonalizationService:
    """Business logic for the personalization endpoints."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.catalog_repo = AgentCatalogRepository(session)
        self.profile_repo = UserAgentRepository(session)
        self.memory_repo = UserMemoryRepository(session)

    # ------------------------------------------------------------------ #
    # Catalog
    # ------------------------------------------------------------------ #
    async def list_groups(self) -> list[AgentGroup]:
        """Return the active catalog groups, in catalog order."""
        return await self.catalog_repo.list_groups(active_only=True)

    async def list_sub_agents(self) -> list[SubAgent]:
        """Return every catalog sub-agent with its resolved group slug."""
        return await self.catalog_repo.list_sub_agents()

    # ------------------------------------------------------------------ #
    # Profile
    # ------------------------------------------------------------------ #
    async def get_profile(self, user_id: int) -> UserAgent | None:
        """Return the user's persisted profile, or ``None`` (defaults apply)."""
        return await self.profile_repo.get_by_user(user_id)

    async def snapshot(self, user_id: int) -> tuple[UserAgent | None, dict[str, AgentGroup], dict[str, SubAgent]]:
        """Return ``(profile, groups_by_slug, sub_agents_by_slug)`` for ``GET``.

        Resolving the catalog entries twice (once here, once while building
        the user-agent response) would cost extra queries, so the router calls
        this once and renders the response from the maps.
        """
        profile = await self.get_profile(user_id)
        if profile is None:
            return None, {}, {}

        groups = {
            group.slug: group
            for group in await self.catalog_repo.groups_by_slugs(profile.active_group_slugs)
            if group.active
        }
        sub_agents = {
            sub.slug: sub
            for sub in await self.catalog_repo.sub_agents_by_slugs(profile.active_sub_agent_slugs)
            if sub.active
        }
        return profile, groups, sub_agents

    async def update_profile(self, user_id: int, command: UpdateAgentProfileCommand) -> UserAgent:
        """Apply a partial profile update (creating the row on first use).

        Raises:
            ValueError: An ``agent_name`` rejected by the name filter, an
                unknown slug, or a sub-agent that requires a higher plan.
            PlanRequiredError: A selected group/sub-agent exceeds the user's
                plan (402/403), enforced from the single ``PlanLimits`` config.
        """
        if command.agent_name is not None:
            reason = blocked_name_reason(command.agent_name)
            if reason is not None:
                raise ValueError(reason)

        async with self.session.begin():
            profile, _ = await self.profile_repo.get_or_create(user_id)

            if command.agent_name is not None:
                profile.agent_name = command.agent_name
            if command.tone is not None:
                profile.tone = command.tone
            if command.language is not None:
                profile.language = command.language
            if command.timezone is not None:
                profile.timezone = command.timezone
            if command.simplified_mode is not None:
                profile.simplified_mode = command.simplified_mode

            requirement = (
                command.active_group_slugs is not None
                or command.active_sub_agent_slugs is not None
            )
            entitlement = (
                await SubscriptionService(self.session).resolve_entitlement(user_id)
                if requirement
                else None
            )

            if command.active_group_slugs is not None:
                groups = await self.catalog_repo.groups_by_slugs(command.active_group_slugs)
                existing = {group.slug for group in groups if group.active}
                requested = set(command.active_group_slugs)
                if requested - existing:
                    unknown = sorted(requested - existing)
                    raise ValueError(f"Grupo(s) desconhecido(s) ou inativo(s): {', '.join(unknown)}")
                if entitlement is not None and requested:
                    enforce_feature(entitlement, FEATURE_GROUPS)
                    enforce_count_limit(
                        entitlement,
                        feature=FEATURE_GROUPS,
                        count=len(requested),
                        limit=entitlement.limits.max_groups,
                        label="grupos ativos",
                    )
                profile.active_group_slugs = sorted(requested)

            if command.active_sub_agent_slugs is not None:
                sub_agents = await self.catalog_repo.sub_agents_by_slugs(command.active_sub_agent_slugs)
                existing_ids = {sub.slug: sub for sub in sub_agents}
                requested = set(command.active_sub_agent_slugs)
                if requested - existing_ids.keys():
                    unknown = sorted(requested - existing_ids.keys())
                    raise ValueError(f"Sub-agente(s) desconhecido(s): {', '.join(unknown)}")
                user_tier_rank = PLAN_RANK.get(entitlement.plan.value, 0) if entitlement else 0
                for sub in sub_agents:
                    if PLAN_RANK.get(sub.min_plan, 0) > user_tier_rank:
                        raise ValueError(
                            f"O sub-agente \"{sub.name}\" exige o plano \"{sub.min_plan}\"."
                        )
                if entitlement is not None and requested:
                    enforce_feature(entitlement, FEATURE_SUB_AGENTS)
                    enforce_count_limit(
                        entitlement,
                        feature=FEATURE_SUB_AGENTS,
                        count=len(requested),
                        limit=entitlement.limits.max_sub_agents,
                        label="sub-agentes ativos",
                    )
                profile.active_sub_agent_slugs = sorted(requested)

            return await self.profile_repo.update(profile)

    # ------------------------------------------------------------------ #
    # Memories
    # ------------------------------------------------------------------ #
    async def list_memories(self, user_id: int) -> list[UserMemory]:
        """Return the user's memories (most recent first)."""
        return await self.memory_repo.list_for_user(user_id)

    async def create_memory(self, user_id: int, command: CreateUserMemoryCommand) -> UserMemory | None:
        """Create a user-owned memory; ``None`` when the key is already taken.

        Raises:
            ValueError: The key uses the reserved ``auto_`` prefix.
        """
        key = command.key.strip()
        if key.startswith(RESERVED_MEMORY_PREFIX):
            raise ValueError(f"A chave \"{key}\" é reservada para memorias aprendidas automaticamente.")

        async with self.session.begin():
            existing = await self.memory_repo.get(user_id, key)
            if existing is not None:
                return None
            return await self.memory_repo.create(user_id, key=key, value=command.value.strip())

    async def update_memory(self, user_id: int, key: str, command: UpdateUserMemoryCommand) -> UserMemory | None:
        """Patch a memory's ``value``/``enabled``; ``None`` when it's missing."""
        async with self.session.begin():
            memory = await self.memory_repo.get(user_id, key)
            if memory is None:
                return None
            value = command.value.strip() if command.value is not None else None
            return await self.memory_repo.update(memory, value=value, enabled=command.enabled)

    async def delete_memory(self, user_id: int, key: str) -> bool:
        """Delete a memory; return whether a row was actually removed."""
        async with self.session.begin():
            return await self.memory_repo.delete(user_id, key)

    # ------------------------------------------------------------------ #
    # Learning toggle
    # ------------------------------------------------------------------ #
    async def set_learning(self, user_id: int, enabled: bool) -> bool:
        """Turn auto-learning on/off (creating the profile row if needed)."""
        async with self.session.begin():
            profile, _ = await self.profile_repo.get_or_create(user_id)
            profile.learning_enabled = enabled
            await self.profile_repo.update(profile)
        return enabled
