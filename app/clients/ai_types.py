"""Shared value objects for the AI routing layer.

These are the neutral data shapes :class:`app.clients.ai_router.AIRouter`
and :func:`app.clients.ai_router.run_ai_request` exchange with the rest of
the application.  Keeping them model-free (no Pydantic, no ORM) lets
services describe a request without importing any provider machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AITaskType(str, Enum):
    """What the caller is asking the AI to do.

    ``strict_json`` requests must return parseable JSON; ``roadmap`` is the
    roadmap-specific JSON generation.  Model selection can key off the task
    so, for example, every fixture that produces brittle JSON always uses a
    model that is known to produce well-formed output.
    """

    CHAT = "chat"
    ROADMAP = "roadmap"
    STRICT_JSON = "strict_json"


class PlanTier(str, Enum):
    """The user's subscription tier, used for quota and model selection.

    ``FREE`` is the always-available limited tier; ``TRIAL`` is the automatic
    new-user trial and behaves like a small paid plan; ``STARTER``/``PRO``/
    ``PREMIUM`` mirror the plans sold through Google Play.  The per-plan
    ceilings live in :mod:`app.clients.plan_limits` (the single source of
    truth); this enum is the vocabulary callers route on.
    """

    FREE = "free"
    TRIAL = "trial"
    STARTER = "starter"
    PRO = "pro"
    PREMIUM = "premium"


@dataclass(frozen=True)
class AIRequest:
    """Everything the AI router needs to serve one completion.

    Attributes:
        system_prompt: System instructions for the assistant.
        user_message: The current user turn.
        task_type: Which kind of generation this is (drives model routing).
        plan_tier: The caller's tier (drives quota and model routing).
        user_id: Owning user for per-plan daily quota tracking.  ``None``
            (anonymous / system) bypasses the quota entirely.
        history: Prior ``{"role", "content"}`` turns for multi-turn chats.
            When empty the request is a single-turn completion.
    """

    system_prompt: str
    user_message: str
    task_type: AITaskType = AITaskType.CHAT
    plan_tier: PlanTier = PlanTier.FREE
    user_id: int | str | None = None
    history: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class AIResponse:
    """The result of one routed AI request.

    Attributes:
        text: The assistant's reply.
        provider: Name of the provider that served it (for logs/telemetry).
        model: Model actually used.
        tokens_in/tokens_out: Reported token usage when the provider echoes
            it (0 when unknown); daily quota is charged on an estimate so
            these are informational.
    """

    text: str
    provider: str
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
