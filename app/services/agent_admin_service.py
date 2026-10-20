"""
Service layer for the admin "Agentes" console.

Owns the create/edit/disable/duplicate/version/rollback workflow for the three
prompt-bearing catalog entities:

* ``group``       → :class:`~app.models.agent_group.AgentGroup`
* ``sub_agent``   → :class:`~app.models.sub_agent.SubAgent`
* ``agent``       → :class:`~app.models.system_prompt.SystemPrompt` (custom-prompt agent)

Every prompt text passes through :mod:`app.clients.prompt_safety` before it is
saved, so an operator cannot smuggle in text that tries to override the
immutable safety preamble.  Each accepted change appends an immutable row to
``prompt_versions`` and invalidates every AI cache that depended on the prompt.
"""

from __future__ import annotations

import re
from difflib import unified_diff
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.prompt_safety import MAX_PROMPT_CHARS, validate_prompt
from app.models.agent_group import AgentGroup
from app.models.prompt_version import (
    ENTITY_AGENT_GROUP,
    ENTITY_SUB_AGENT,
    ENTITY_SYSTEM_PROMPT,
    PromptVersion,
)
from app.models.sub_agent import SubAgent
from app.models.system_prompt import SystemPrompt
from app.repositories.agent_catalog_repository import AgentCatalogRepository
from app.repositories.prompt_version_repository import PromptVersionRepository
from app.repositories.system_prompt_repository import SystemPromptRepository

#: Public entity key → version-row discriminator.
ENTITY_VERSION_NAME = {
    "group": ENTITY_AGENT_GROUP,
    "sub_agent": ENTITY_SUB_AGENT,
    "agent": ENTITY_SYSTEM_PROMPT,
}

#: Prefixes of the AI cache keys that depend on an admin-authored prompt.
_CONVERSATION_PREFIX = "conversation:"
_AI_RESPONSE_PREFIX = "ai_response:"


def _slugify(value: str) -> str:
    """Turn a name into a URL-safe slug (lowercase, ``_`` separators)."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "copiado"


class AgentAdminService:
    """Business logic for the agent catalog console.

    Args:
        session: An open SQLAlchemy :class:`AsyncSession`.
        redis_client: Optional cache client used to invalidate prompt-dependent
            AI caches.  When omitted, cache invalidation is skipped (useful in
            unit tests that do not exercise Redis).
    """

    def __init__(self, session: AsyncSession, redis_client: Any | None = None) -> None:
        self.session = session
        self.redis = redis_client
        self.catalog = AgentCatalogRepository(session)
        self.prompts = SystemPromptRepository(session)
        self.versions = PromptVersionRepository(session)

    # ------------------------------------------------------------------ #
    # Listing / lookup
    # ------------------------------------------------------------------ #
    async def list_groups(self) -> list[AgentGroup]:
        return await self.catalog.list_groups(active_only=False)

    async def list_sub_agents(self) -> list[SubAgent]:
        return await self.catalog.list_sub_agents(active_only=False)

    async def list_agents(self) -> list[SystemPrompt]:
        return await self.prompts.list(offset=0, limit=1000)

    async def get(self, entity: str, entity_id: int) -> Any:
        """Return one catalog row, or raise ``404`` when it is unknown."""
        row = await self._get(entity, entity_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Item nao encontrado.")
        return row

    async def _get(self, entity: str, entity_id: int) -> Any:
        if entity == "group":
            return await self.catalog.get_group_by_id(entity_id)
        if entity == "sub_agent":
            return await self.catalog.get_sub_agent_by_id(entity_id)
        if entity == "agent":
            return await self.prompts.get_by_id(entity_id)
        raise HTTPException(status_code=404, detail="Item nao encontrado.")

    # ------------------------------------------------------------------ #
    # Safety
    # ------------------------------------------------------------------ #
    @staticmethod
    def validate_prompt(text: str, max_chars: int = MAX_PROMPT_CHARS) -> str | None:
        """Return a PT-BR violation message, or ``None`` when the text is safe."""
        return validate_prompt(text, max_chars=max_chars)

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    async def create(self, entity: str, values: dict[str, Any], *, admin_id: int | None, max_chars: int) -> Any:
        """Create a catalog row and record its first prompt version."""
        error = self.validate_prompt(str(values.get("system_prompt", "")), max_chars)
        if error is not None:
            raise HTTPException(status_code=422, detail=error)

        row: Any
        if entity == "group":
            if not str(values.get("slug", "")).strip():
                raise HTTPException(status_code=422, detail="O identificador (slug) e obrigatorio.")
            if await self.catalog.get_group(str(values["slug"])) is not None:
                raise HTTPException(status_code=422, detail="Ja existe um grupo com este identificador.")
            row = await self.catalog.create_group(**values)
        elif entity == "sub_agent":
            if not str(values.get("slug", "")).strip():
                raise HTTPException(status_code=422, detail="O identificador (slug) e obrigatorio.")
            if await self.catalog.get_sub_agent(str(values["slug"])) is not None:
                raise HTTPException(status_code=422, detail="Ja existe um sub-agente com este identificador.")
            row = await self.catalog.create_sub_agent(**values)
        elif entity == "agent":
            row = SystemPrompt(**values)
            self.session.add(row)
            await self.session.commit()
            await self.session.refresh(row)
        else:
            raise HTTPException(status_code=404, detail="Entidade invalida.")

        await self.versions.add(
            entity=ENTITY_VERSION_NAME[entity],
            entity_id=int(row.id),
            content=str(row.system_prompt),
            note="criacao",
            created_by=admin_id,
        )
        await self._invalidate(entity, int(row.id))
        return row

    async def update(
        self,
        entity: str,
        entity_id: int,
        values: dict[str, Any],
        *,
        admin_id: int | None,
        max_chars: int,
        note: str | None = None,
    ) -> Any:
        """Patch a row and append a prompt version when the text is present."""
        await self.get(entity, entity_id)
        if "system_prompt" in values:
            error = self.validate_prompt(str(values["system_prompt"]), max_chars)
            if error is not None:
                raise HTTPException(status_code=422, detail=error)

        row: Any
        if entity == "group":
            row = await self.catalog.update_group(entity_id, **values)
        elif entity == "sub_agent":
            row = await self.catalog.update_sub_agent(entity_id, **values)
        elif entity == "agent":
            row = await self.prompts.update_fields(entity_id, **values)
        else:
            raise HTTPException(status_code=404, detail="Entidade invalida.")

        # Only a prompt change creates a new version and busts the caches; an
        # active/sort/metadata tweak does not invalidate the AI caches.
        if "system_prompt" in values:
            await self.versions.add(
                entity=ENTITY_VERSION_NAME[entity],
                entity_id=entity_id,
                content=str(row.system_prompt),
                note=note or "edicao",
                created_by=admin_id,
            )
            await self._invalidate(entity, entity_id)
        return row

    async def set_active(self, entity: str, entity_id: int, active: bool) -> Any:
        """Enable or disable a catalog row (inactive rows leave the public list).

        Visibility changes bust the cache too: a disabled entity must stop
        being folded into prompts, and a re-enabled one must be picked up.
        """
        row = await self.update(
            entity,
            entity_id,
            {"active": active},
            admin_id=None,
            max_chars=MAX_PROMPT_CHARS,
            note=None,
        )
        await self._invalidate(entity, entity_id)
        return row

    async def duplicate(self, entity: str, entity_id: int, *, admin_id: int | None, max_chars: int) -> Any:
        """Clone a row as an inactive draft with a unique slug and `` (copia)`` name."""
        original = await self.get(entity, entity_id)
        if entity == "group":
            base_slug = str(original.slug)
            slug = await self._unique_group_slug(f"{base_slug}_copia")
            values = {
                "slug": slug,
                "name": f"{original.name} (copia)",
                "description": original.description,
                "icon": original.icon,
                "audience": original.audience,
                "active": False,
                "sort_order": original.sort_order,
                "system_prompt": original.system_prompt,
            }
        elif entity == "sub_agent":
            slug = await self._unique_sub_agent_slug(f"{original.slug}_copia")
            values = {
                "slug": slug,
                "name": f"{original.name} (copia)",
                "description": original.description,
                "group_id": original.group_id,
                "system_prompt": original.system_prompt,
                "min_plan": original.min_plan,
                "active": False,
                "sort_order": original.sort_order,
                "icon": original.icon,
                "audience": original.audience,
            }
        elif entity == "agent":
            values = {
                "tipo": f"{original.tipo} (copia)",
                "system_prompt": original.system_prompt,
                "active": False,
                "sort_order": original.sort_order,
                "icon": original.icon,
                "description": original.description,
                "min_plan": original.min_plan,
                "audience": original.audience,
            }
        else:
            raise HTTPException(status_code=404, detail="Entidade invalida.")

        return await self.create(entity, values, admin_id=admin_id, max_chars=max_chars)

    async def rollback(
        self,
        entity: str,
        entity_id: int,
        version_id: int,
        *,
        admin_id: int | None,
        max_chars: int,
    ) -> Any:
        """Restore a previous version by appending a new snapshot with its content."""
        target = await self.versions.get(version_id)
        if target is None or target.entity != ENTITY_VERSION_NAME[entity] or target.entity_id != entity_id:
            raise HTTPException(status_code=404, detail="Versao nao encontrada.")
        await self.get(entity, entity_id)
        return await self.update(
            entity,
            entity_id,
            {"system_prompt": target.content},
            admin_id=admin_id,
            max_chars=max_chars,
            note=f"rollback para v{target.version}",
        )

    # ------------------------------------------------------------------ #
    # Versions / diff
    # ------------------------------------------------------------------ #
    async def list_versions(self, entity: str, entity_id: int) -> list[PromptVersion]:
        await self.get(entity, entity_id)
        return await self.versions.list_for(ENTITY_VERSION_NAME[entity], entity_id)

    @staticmethod
    def diffs(versions: list[PromptVersion]) -> dict[int, list[str]]:
        """Return ``{version_id: unified_diff_lines}`` against the older version.

        The newest version has no predecessor in the list, so its diff is
        computed against the second-newest; the oldest renders an empty diff.
        """
        ordered = sorted(versions, key=lambda row: row.version)
        result: dict[int, list[str]] = {}
        for index, row in enumerate(ordered):
            if index == 0:
                result[row.id] = []
                continue
            previous = ordered[index - 1]
            result[row.id] = list(
                unified_diff(
                    previous.content.splitlines(),
                    row.content.splitlines(),
                    fromfile=f"v{previous.version}",
                    tofile=f"v{row.version}",
                    lineterm="",
                )
            )
        return result

    # ------------------------------------------------------------------ #
    # Cache invalidation
    # ------------------------------------------------------------------ #
    async def _invalidate(self, entity: str, entity_id: int) -> None:
        """Drop every AI cache entry that depended on the changed prompt.

        A custom-prompt *agent* only affects its own conversation namespace; a
        group/sub-agent fragment is folded into **every** personalized prompt,
        so its change invalidates all conversation/response caches.
        """
        if self.redis is None:
            return
        if entity == "agent":
            patterns = [
                f"{_CONVERSATION_PREFIX}{entity_id}:*",
                f"{_AI_RESPONSE_PREFIX}{entity_id}:*",
            ]
        else:
            patterns = [f"{_CONVERSATION_PREFIX}*", f"{_AI_RESPONSE_PREFIX}*"]
        keys: list[str] = []
        for pattern in patterns:
            keys.extend(await self.redis.scan_keys(pattern))
        await self.redis.delete_many(keys)

    # ------------------------------------------------------------------ #
    # Slug helpers
    # ------------------------------------------------------------------ #
    async def _unique_group_slug(self, base: str) -> str:
        candidate = base
        suffix = 2
        while await self.catalog.get_group(candidate) is not None:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    async def _unique_sub_agent_slug(self, base: str) -> str:
        candidate = base
        suffix = 2
        while await self.catalog.get_sub_agent(candidate) is not None:
            candidate = f"{base}_{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def slugify(value: str) -> str:
        """Expose slug generation for the create form's default value."""
        return _slugify(value)
