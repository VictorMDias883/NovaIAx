"""Request/response schemas for the agent tool-orchestration endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from app.models.agent_action import AgentActionStatus, AgentActionType

#: Where an action runs: ``backend`` (server executes after confirmation) or
#: ``device`` (the app executes locally and reports the result back).
Executor = Literal["backend", "device"]


class AgentChatRequest(BaseModel):
    """Body for ``POST /agents/chat``.

    ``message`` and the legacy ``user_message`` are accepted interchangeably.
    """

    message: str = Field(
        min_length=1,
        max_length=4000,
        validation_alias=AliasChoices("message", "user_message"),
    )


class EmergencyContact(BaseModel):
    """One helpline surfaced when a crisis/emergency is detected."""

    name: str
    description: str = ""
    phone: str


class EmergencyBlock(BaseModel):
    """Machine-readable emergency resources attached to a chat reply.

    ``active`` is ``True`` only on a crisis/emergency turn; ``contacts`` then
    lists the Brazilian helplines (CVV 188, SAMU 192, Bombeiros 193, Polícia
    190, Ligue 180) so the app can render tappable call actions.  On an
    ordinary turn ``active`` is ``False`` and ``contacts`` is empty.
    """

    active: bool = False
    contacts: list[EmergencyContact] = Field(default_factory=list)


class SafetyNotice(BaseModel):
    """Safety-hook outcome attached to a chat reply."""

    crisis: bool = False
    disclaimers: list[str] = Field(default_factory=list)
    emergency: EmergencyBlock = Field(default_factory=EmergencyBlock)


class ProposedActionResponse(BaseModel):
    """One action the model proposed, already validated and persisted.

    ``executor`` is ``"device"`` when the app must run it locally and report
    the outcome back, or ``"backend"`` when the server runs it on
    ``POST /agents/actions/{id}/confirm``.  ``requires_confirmation`` and
    ``prepares_only`` are server-side policy (see
    :mod:`app.schemas.agent_action_schemas`).
    """

    id: int
    type: AgentActionType
    params: dict[str, Any]
    requires_confirmation: bool
    summary: str
    executor: Executor = "backend"
    is_device: bool
    prepares_only: bool

    @model_validator(mode="after")
    def _derive_executor(self) -> ProposedActionResponse:
        self.executor = "device" if self.is_device else "backend"
        return self


class AgentChatResponse(BaseModel):
    """Result of one orchestrated agent turn."""

    reply: str
    why: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    proposed_actions: list[ProposedActionResponse] = Field(default_factory=list)
    message_id: str
    safety: SafetyNotice = Field(default_factory=SafetyNotice)


class AgentActionResponse(BaseModel):
    """State of one action after confirm/reject/report-result."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    type: AgentActionType
    status: AgentActionStatus
    summary: str
    requires_confirmation: bool
    executor: Executor = "backend"
    is_device: bool
    prepares_only: bool
    params: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None
    error_code: str | None = None
    detail: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def _derive_executor(self) -> AgentActionResponse:
        self.executor = "device" if self.is_device else "backend"
        return self


class AgentActionConfirmRequest(BaseModel):
    """Optional body for ``POST /agents/actions/{id}/confirm``.

    Empty today; declared so the client can send (and the spec can document)
    an explicit, typed body without a breaking change later.
    """

    model_config = ConfigDict(extra="forbid")


class AgentActionRejectRequest(BaseModel):
    """Optional body for ``POST /agents/actions/{id}/reject``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class AgentActionReportResultRequest(BaseModel):
    """Body for ``POST /agents/actions/{id}/result`` (device actions only).

    ``status`` is what the app observed locally: ``executed``, ``failed`` or
    ``canceled`` (the user dismissed the confirmation prompt).  ``error_code``
    is a stable machine code (e.g. ``permission_denied``) and ``detail`` an
    optional human string; ``note`` is kept as a deprecated short alias.
    """

    status: Literal["executed", "failed", "canceled"]
    error_code: str | None = Field(default=None, max_length=60)
    detail: str | None = Field(default=None, max_length=1000)
    note: str | None = Field(default=None, max_length=500, deprecated=True)
