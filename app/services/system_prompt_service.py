"""
Service layer for system prompt administration.

This service is responsible for enforcing business rules and
orchestrating repository operations for system prompts.

Read operations accept an ``include_text`` flag.  The two read endpoints are
open to any authenticated user, but the prompt text is the agent's own
instruction set, so only administrators receive it; everyone else gets the
record metadata alone.
"""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.prompt_safety import validate_prompt
from app.models.system_prompt import SystemPrompt
from app.repositories.system_prompt_repository import SystemPromptRepository


class SystemPromptService:
    """Business logic for administrator-managed system prompts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = SystemPromptRepository(session)

    @staticmethod
    def _serialize(prompt: SystemPrompt, *, include_text: bool) -> dict[str, object]:
        """Return a prompt as a response payload.

        The ``system_prompt`` key is omitted entirely when ``include_text`` is
        false, so a non-admin response cannot leak the text by accident (an
        empty or masked string would still confirm its presence and length).
        """
        payload: dict[str, object] = {
            "id": prompt.id,
            "tipo": prompt.tipo,
            "active": prompt.active,
            "sort_order": prompt.sort_order,
            "icon": prompt.icon,
            "description": prompt.description,
            "min_plan": prompt.min_plan,
            "audience": prompt.audience,
            "created_at": prompt.created_at,
        }
        if include_text:
            payload["system_prompt"] = prompt.system_prompt
        return payload

    async def list_prompts(
        self, page: int = 1, limit: int = 50, include_text: bool = True, active_only: bool = True
    ) -> dict[str, object]:
        """Return a page of prompts, with or without the prompt text.

        The public agents list shows only active prompts, in catalog order.
        """
        offset = (page - 1) * limit
        prompts = await self.repo.list(offset=offset, limit=limit, active_only=active_only)
        total = await self.repo.count(active_only=active_only)
        return {
            "prompts": [self._serialize(prompt, include_text=include_text) for prompt in prompts],
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def get_prompt(self, prompt_id: int, include_text: bool = True) -> dict[str, object]:
        """Return one prompt, with or without the prompt text.

        Raises:
            HTTPException(404): If no prompt has that id.
        """
        prompt = await self.repo.get_by_id(prompt_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="Prompt de sistema não encontrado.")
        return self._serialize(prompt, include_text=include_text)

    async def create_prompt(
        self,
        tipo: str,
        system_prompt: str,
        *,
        description: str = "",
        icon: str = "🤖",
        min_plan: str = "free",
        audience: str = "",
        active: bool = True,
        sort_order: int = 0,
    ) -> dict[str, object]:
        error = validate_prompt(system_prompt)
        if error is not None:
            raise HTTPException(status_code=422, detail=error)
        prompt = SystemPrompt(
            tipo=tipo,
            system_prompt=system_prompt,
            description=description,
            icon=icon,
            min_plan=min_plan,
            audience=audience,
            active=active,
            sort_order=sort_order,
        )
        self.session.add(prompt)
        await self.session.commit()
        await self.session.refresh(prompt)
        return self._serialize(prompt, include_text=True)

    async def update_prompt(
        self,
        prompt_id: int,
        tipo: str,
        system_prompt: str,
        *,
        description: str | None = None,
        icon: str | None = None,
        min_plan: str | None = None,
        audience: str | None = None,
        active: bool | None = None,
        sort_order: int | None = None,
    ) -> dict[str, object]:
        error = validate_prompt(system_prompt)
        if error is not None:
            raise HTTPException(status_code=422, detail=error)
        values: dict[str, object] = {"tipo": tipo, "system_prompt": system_prompt}
        for key, value in (
            ("description", description),
            ("icon", icon),
            ("min_plan", min_plan),
            ("audience", audience),
            ("active", active),
            ("sort_order", sort_order),
        ):
            if value is not None:
                values[key] = value
        prompt = await self.repo.update_fields(prompt_id, **values)
        if prompt is None:
            raise HTTPException(status_code=404, detail="Prompt de sistema não encontrado.")
        return self._serialize(prompt, include_text=True)

    async def delete_prompt(self, prompt_id: int) -> None:
        prompt = await self.repo.get_by_id(prompt_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="Prompt de sistema não encontrado.")
        await self.repo.delete(prompt_id)
