"""Service layer for AI chat completion orchestration.

This service retrieves the configured system prompt, restores the previous
conversation held with that agent, builds the chat payload and delegates the
request to an AI client implementation.

Conversation history is persisted per ``(agent, user)`` through
:class:`~app.cache.conversation_cache.ConversationCache` under the key
``conversation:<agent_id>:<user_id>``, so a multi-turn chat with the same
agent keeps its context while two users — or the same user with two different
agents — never share a thread.

Responses are deliberately **not** memoised on a hash of the message text.
Replaying an identical message must still be answered by the model against the
accumulated history; a response cache keyed on the message alone would serve
an answer produced in an earlier context and freeze the conversation.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.conversation_cache import ConversationCache
from app.cache.redis_client import RedisClient
from app.clients.ai_client import AIClient
from app.clients.ai_router import AIRouter, run_ai_request
from app.clients.ai_types import AIRequest, PlanTier
from app.commands.chat_completion_command import ChatCompletionCommand
from app.commands.clear_chat_conversation_command import ClearChatConversationCommand
from app.core.logging import get_logger
from app.models.chat_message import ChatAgent
from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.system_prompt_repository import SystemPromptRepository
from app.services.agent_prompt_service import (
    build_personalized_system_prompt,
    load_personalization_context,
    parse_personalization_block,
)
from app.services.memory_learning_service import MemoryLearningService

logger = get_logger(__name__)


class ChatCompletionService:
    """Business logic for AI chat completion requests."""

    def __init__(
        self,
        session: AsyncSession,
        ai_client: AIClient | AIRouter,
        cache: RedisClient | None = None,
    ) -> None:
        self.session = session
        self.ai_client = ai_client
        self.cache = cache
        self.prompt_repo = SystemPromptRepository(session)
        self.chat_messages = ChatMessageRepository(session)
        self.conversation_cache = ConversationCache(cache)

    @staticmethod
    def agent_key(agent_id: int) -> str:
        """Return the conversation-cache namespace used for an agent id.

        The system-prompt id doubles as the agent name, which is what the
        existing key layout ``conversation:<agent_id>:<user_id>`` encodes.
        """
        return str(agent_id)

    async def create_completion(
        self,
        command: ChatCompletionCommand,
        user_id: int | str,
        plan_tier: PlanTier = PlanTier.FREE,
    ) -> dict[str, object]:
        """Answer one chat turn, keeping the per-user, per-agent history.

        The stored history is replayed to the model together with the new user
        message, and the exchange is appended to the cache so the next turn
        resumes the same thread.

        Personalization (profile, memories, auto-learning) applies only to
        real database users; API-key identities (``user_id == "api-key"``)
        get the plain admin-managed system prompt.
        """
        prompt = await self.prompt_repo.get_by_id(command.agent_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="Agente não encontrado.")

        user_int = user_id if isinstance(user_id, int) else None
        if user_int is None and isinstance(user_id, str) and user_id.isdigit():
            user_int = int(user_id)

        message_id = uuid.uuid4().hex
        if user_int is not None:
            await self.chat_messages.create(
                user_int, message_id=message_id, agent=ChatAgent.AI
            )

        system_prompt = prompt.system_prompt
        if user_int is not None:
            ctx = await load_personalization_context(self.session, user_int)
            if ctx is not None:
                system_prompt = build_personalized_system_prompt(system_prompt, ctx)

        agent = self.agent_key(command.agent_id)
        history = await self.conversation_cache.get_messages(agent, user_id)
        to_send = [*history, {"role": "user", "content": command.user_message}]

        assistant = await run_ai_request(
            self.ai_client,
            AIRequest(
                system_prompt=system_prompt,
                user_message=command.user_message,
                plan_tier=plan_tier,
                user_id=user_id,
                history=to_send,
            ),
        )
        assistant_message, why, sources = parse_personalization_block(assistant.text)

        await self.conversation_cache.save(
            agent,
            user_id,
            [*to_send, {"role": "assistant", "content": assistant_message}],
        )

        if user_int is not None:
            try:
                await MemoryLearningService(self.session).learn_from_statement(user_int, command.user_message)
            except Exception:  # pragma: no cover - defensive; a turn must not fail on learning
                logger.exception("Auto-learning failed for user %s", user_int)

        return {
            "assistant_message": assistant_message,
            "why": why,
            "sources": sources,
            "message_id": message_id,
        }

    async def clear_conversation(self, command: ClearChatConversationCommand, user_id: int) -> None:
        """Drop the caller's cached history with one agent.

        Raises:
            HTTPException(404): If ``agent_id`` does not match a system prompt.
        """
        prompt = await self.prompt_repo.get_by_id(command.agent_id)
        if prompt is None:
            raise HTTPException(status_code=404, detail="Agente não encontrado.")
        await self.conversation_cache.clear(self.agent_key(command.agent_id), user_id)
