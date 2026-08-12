"""
Service layer for AI chat completion orchestration.

This service retrieves the configured system prompt, builds the chat
payload, and delegates the request to an AI client implementation.
"""

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_client import AIClient
from app.commands.chat_completion_command import ChatCompletionCommand
from app.repositories.system_prompt_repository import SystemPromptRepository


class ChatCompletionService:
    """Business logic for AI chat completion requests."""

    def __init__(self, session: AsyncSession, ai_client: AIClient) -> None:
        self.session = session
        self.ai_client = ai_client
        self.prompt_repo = SystemPromptRepository(session)

    async def create_completion(self, command: ChatCompletionCommand) -> dict[str, str]:
        """Create a chat completion response for the given agent and user message."""
        prompt = await self.prompt_repo.get_by_id(command.agent_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="Agent not found")

        assistant_message = await self.ai_client.create_chat_completion(
            system_prompt=prompt.system_prompt,
            user_message=command.user_message,
        )

        return {"assistant_message": assistant_message}
