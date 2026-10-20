"""
Pydantic schemas for the agent personalization domain.

Covers the catalog endpoints (``GET /agent-groups``, ``GET /sub-agents``),
the per-user profile (``GET/PUT /me/agent``), the memory CRUD
(``GET/POST/PATCH/DELETE /me/memories``) and the learning toggle
(``PUT /me/learning``).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentGroupResponse(BaseModel):
    """One entry of the audience-group catalog (``GET /agent-groups``)."""

    slug: str
    name: str
    description: str
    icon: str
    audience: str
    sort_order: int


class SubAgentResponse(BaseModel):
    """One entry of the sub-agent catalog (``GET /sub-agents``)."""

    slug: str
    name: str
    description: str
    group_slug: str | None = None
    min_plan: str
    icon: str = "🧩"
    audience: str = ""
    sort_order: int = 0


class UserAgentResponse(BaseModel):
    """The authenticated user's personalization profile (``GET /me/agent``).

    ``active_group_slugs``/``active_sub_agent_slugs`` carry the raw selected
    slugs (persisted form); ``active_groups``/``active_sub_agents`` carry the
    resolved catalog entries so the client does not need a second request.
    """

    agent_name: str
    tone: str | None = None
    language: str
    timezone: str
    active_group_slugs: list[str]
    active_sub_agent_slugs: list[str]
    simplified_mode: bool
    learning_enabled: bool
    active_groups: list[AgentGroupResponse]
    active_sub_agents: list[SubAgentResponse]


class UserAgentUpdate(BaseModel):
    """Request body for ``PUT /me/agent``.

    Every field is optional so a single PUT can patch one attribute; the
    service applies only the provided values.
    """

    model_config = ConfigDict(extra="forbid")

    agent_name: str | None = Field(default=None, min_length=2, max_length=30)
    tone: str | None = Field(default=None, min_length=1, max_length=80)
    language: str | None = Field(default=None, min_length=2, max_length=10)
    timezone: str | None = Field(default=None, min_length=1, max_length=50)
    active_group_slugs: list[str] | None = None
    active_sub_agent_slugs: list[str] | None = None
    simplified_mode: bool | None = None


class MemoryCreate(BaseModel):
    """Request body for ``POST /me/memories``."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=500)


class MemoryUpdate(BaseModel):
    """Request body for ``PATCH /me/memories/{key}``.

    At least one of ``value``/``enabled`` must be provided.
    """

    model_config = ConfigDict(extra="forbid")

    value: str | None = Field(default=None, min_length=1, max_length=500)
    enabled: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "MemoryUpdate":
        if self.value is None and self.enabled is None:
            raise ValueError("informe value ou enabled")
        return self


class MemoryResponse(BaseModel):
    """One per-user memory (``GET /me/memories``)."""

    key: str
    value: str
    source: str
    enabled: bool
    created_at: datetime


class LearningUpdate(BaseModel):
    """Request body for ``PUT /me/learning``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


class LearningResponse(BaseModel):
    """Response body after toggling learning."""

    enabled: bool
