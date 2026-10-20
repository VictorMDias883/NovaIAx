"""
ORM model for the ``agent_groups`` table.

Agent groups are the personalization catalog entry point: each one describes a
life audience (single mothers, elderly, truck drivers, ...) with a PT-BR
description, an icon, and a tailored system-prompt fragment that is folded
into the assistant's prompt when the user activates the group.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class AgentGroup(Base):
    """ORM model representing a personalization audience group.

    Table: ``agent_groups``

    Attributes:
        slug: Stable unique identifier (used in ``user_agents`` selection).
        name: PT-BR display name (e.g. "Mães Solteiras").
        description: PT-BR description shown in the catalog.
        icon: Short emoji/label used by the app UI.
        audience: PT-BR target audience summary.
        active: Whether the group is selectable/listed.
        sort_order: Ordering inside the catalog (lower first).
        system_prompt: Tailored system-prompt fragment folded into the
            assistant's prompt when the group is active.
    """

    __tablename__ = "agent_groups"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default="🧩")
    audience: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
