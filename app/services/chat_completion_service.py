"""Service layer for AI chat completion orchestration.

This service retrieves the configured system prompt, builds the chat
payload, and delegates the request to an AI client implementation.

When a Redis client is supplied, responses are cached for identical
(agent, user-message) prompts using the ``CACHE_TTL_AI`` setting, so
repeated requests within the TTL window are served from cache instead
of hitting the AI provider again.
"""

import hashlib

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.redis_client import RedisClient
from app.clients.ai_client import AIClient
from app.commands.chat_completion_command import ChatCompletionCommand
from app.core.config import get_settings
from app.repositories.system_prompt_repository import SystemPromptRepository


class ChatCompletionService:
    """Business logic for AI chat completion requests."""

    def __init__(
        self,
        session: AsyncSession,
        ai_client: AIClient,
        cache: RedisClient | None = None,
    ) -> None:
        self.session = session
        self.ai_client = ai_client
        self.cache = cache
        self.settings = get_settings()
        self.prompt_repo = SystemPromptRepository(session)

    async def create_completion(self, command: ChatCompletionCommand) -> dict[str, str]:
        """Create a chat completion response for the given agent and user message."""
        prompt = await self.prompt_repo.get_by_id(command.agent_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        cache_key = None
        if self.cache is not None:
            digest = hashlib.sha256(command.user_message.encode("utf-8")).hexdigest()
            cache_key = f"ai_response:{command.agent_id}:{digest}"
            cached = await self.cache.get(cache_key)
            if cached:
                return {"assistant_message": cached}

        assistant_message = await self.ai_client.create_chat_completion(
            system_prompt=prompt.system_prompt,
            user_message=command.user_message,
        )

        if self.cache is not None and cache_key is not None:
            await self.cache.set(cache_key, assistant_message, ex=self.settings.cache_ttl_ai)

        return {"assistant_message": assistant_message}
