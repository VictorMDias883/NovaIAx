"""
ORM model for the ``prompt_versions`` table.

Every admin-authored prompt fragment (agent-group, sub-agent or system prompt)
is versioned: each save appends an immutable row here, and a rollback appends a
*new* row whose content matches the restored version.  History is therefore
append-only and the full evolution of a prompt stays auditable.

``entity`` is one of :data:`PROMPT_ENTITIES` and ``entity_id`` points at the
row in the corresponding table (``agent_groups``, ``sub_agents`` or
``system_prompts``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

#: Entity discriminators stored in ``prompt_versions.entity``.
ENTITY_AGENT_GROUP = "agent_group"
ENTITY_SUB_AGENT = "sub_agent"
ENTITY_SYSTEM_PROMPT = "system_prompt"
PROMPT_ENTITIES = (ENTITY_AGENT_GROUP, ENTITY_SUB_AGENT, ENTITY_SYSTEM_PROMPT)


class PromptVersion(Base):
    """One immutable snapshot of an admin-authored prompt fragment.

    Table: ``prompt_versions``
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("entity", "entity_id", "version", name="uq_prompt_versions_entity_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    entity: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
