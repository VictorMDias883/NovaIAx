"""
ORM model for the ``sub_agents`` table.

Sub-agents are focused capability modules (finances, study, health, ...) that
a user can activate on top of the audience groups.  Each one carries its own
tailored system prompt and a minimum plan tier required to use it.
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class SubAgent(Base):
    """ORM model representing a sub-agent capability module.

    Table: ``sub_agents``

    Attributes:
        slug: Stable unique identifier.
        name: PT-BR display name (e.g. "Finanças").
        description: PT-BR description shown in the catalog.
        group_id: Optional link to a :class:`AgentGroup` the sub-agent is
            most associated with (``None`` for cross-cutting capabilities).
        system_prompt: Tailored system-prompt fragment active when the user
            enables this sub-agent (e.g. health limits itself to reminders,
            never diagnoses).
        min_plan: Minimum :class:`~app.clients.ai_types.PlanTier` required
            (``free`` / ``plus`` / ``pro``).  All seeded sub-agents are
            ``free``; gating is enforced on activation.
    """

    __tablename__ = "sub_agents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_groups.id", ondelete="CASCADE"), nullable=True, index=True
    )
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    min_plan: Mapped[str] = mapped_column(String(20), nullable=False, default="free")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default="🧩")
    audience: Mapped[str] = mapped_column(String(120), nullable=False, default="")
