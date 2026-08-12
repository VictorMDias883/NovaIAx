"""
Service layer for system prompt administration.

This service is responsible for enforcing business rules and
orchestrating repository operations for system prompts.
"""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.system_prompt_repository import SystemPromptRepository


class SystemPromptService:
    """Business logic for administrator-managed system prompts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = SystemPromptRepository(session)

    async def list_prompts(self, page: int = 1, limit: int = 50) -> dict[str, object]:
        offset = (page - 1) * limit
        prompts = await self.repo.list(offset=offset, limit=limit)
        total = await self.repo.count()
        return {
            "prompts": [
                {
                    "id": prompt.id,
                    "tipo": prompt.tipo,
                    "system_prompt": prompt.system_prompt,
                    "created_at": prompt.created_at,
                }
                for prompt in prompts
            ],
            "page": page,
            "limit": limit,
            "total": total,
        }

    async def get_prompt(self, prompt_id: int) -> dict[str, object]:
        prompt = await self.repo.get_by_id(prompt_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="System prompt not found")
        return {
            "id": prompt.id,
            "tipo": prompt.tipo,
            "system_prompt": prompt.system_prompt,
            "created_at": prompt.created_at,
        }

    async def create_prompt(self, tipo: str, system_prompt: str) -> dict[str, object]:
        prompt = await self.repo.create(tipo=tipo, system_prompt=system_prompt)
        return {
            "id": prompt.id,
            "tipo": prompt.tipo,
            "system_prompt": prompt.system_prompt,
            "created_at": prompt.created_at,
        }

    async def update_prompt(self, prompt_id: int, tipo: str, system_prompt: str) -> dict[str, object]:
        prompt = await self.repo.update(prompt_id, tipo=tipo, system_prompt=system_prompt)
        if prompt is None:
            raise HTTPException(status_code=404, detail="System prompt not found")
        return {
            "id": prompt.id,
            "tipo": prompt.tipo,
            "system_prompt": prompt.system_prompt,
            "created_at": prompt.created_at,
        }

    async def delete_prompt(self, prompt_id: int) -> None:
        prompt = await self.repo.get_by_id(prompt_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="System prompt not found")
        await self.repo.delete(prompt_id)
