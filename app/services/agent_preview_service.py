"""
Admin-only "Testar agente" sandbox.

Sends one sample message through the production AI router using an **unsaved
draft** prompt, so an operator can see how an agent behaves before saving.

Guarantees:

* the immutable safety preamble is prepended exactly as it would be in
  production (:func:`app.clients.prompt_safety.with_safety_preamble`);
* the request is exempt from the end-user daily quota (``user_id=None``) — the
  sandbox never touches a real user's budget;
* it is instead rate-limited per admin (Redis counter) to protect the shared
  providers;
* nothing is stored: no conversation history is read or written, and the
  prompt is not persisted here.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.clients.ai_router import AIRouter, run_ai_request
from app.clients.ai_types import AIRequest, AITaskType, PlanTier
from app.clients.prompt_safety import with_safety_preamble

#: Redis key namespace for the per-admin preview counter.
_PREVIEW_KEY_PREFIX = "admin_preview:"

#: Hard cap on the sample message length (defense against prompt stuffing).
MAX_SAMPLE_CHARS = 2000


class AgentPreviewService:
    """Run a draft prompt through the router, rate-limited and side-effect free."""

    def __init__(self, ai_router: AIRouter, redis_client: Any, settings: Any) -> None:
        self.ai_router = ai_router
        self.redis = redis_client
        self.settings = settings

    async def check_rate_limit(self, admin_id: int) -> None:
        """Raise ``429`` when the admin exhausted their preview budget.

        The counter is armed with ``agent_preview_rate_window_seconds`` on the
        first hit of a window and never slid forward, so it behaves like a
        fixed window rather than a rolling one.
        """
        limit = self.settings.agent_preview_rate_limit
        if limit <= 0:
            return
        key = f"{_PREVIEW_KEY_PREFIX}{admin_id}"
        current = await self.redis.incrby(key, 1)
        if await self.redis.ttl(key) == -1:
            await self.redis.expire(key, self.settings.agent_preview_rate_window_seconds)
        if current > limit:
            raise HTTPException(
                status_code=429,
                detail="Muitas tentativas de teste. Aguarde alguns instantes.",
            )

    async def run(self, draft_prompt: str, sample_message: str) -> dict[str, str]:
        """Return the assistant reply for the draft prompt and sample message."""
        system_prompt = with_safety_preamble(draft_prompt)
        response = await run_ai_request(
            self.ai_router,
            AIRequest(
                system_prompt=system_prompt,
                user_message=sample_message[:MAX_SAMPLE_CHARS],
                task_type=AITaskType.CHAT,
                plan_tier=PlanTier.PREMIUM,
                user_id=None,
            ),
        )
        return {
            "reply": response.text,
            "provider": response.provider,
            "model": response.model,
        }
