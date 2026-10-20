"""
SQLAlchemy ORM models for the study domain.

Owner-scoped, UTC datetimes.  Flashcards use a lightweight **SM-2-style**
spaced-repetition schedule: each card stores its ease factor, interval and
repetition count, and the next ``due_at`` is computed from the grade the user
gives on review (see :mod:`app.services.study_service`).  Quiz generation and
mind maps are produced by the AI and returned, never persisted as authoritative
content.

The AI never presents generated study material as a medical/legal/financial
source and every generated payload carries a disclaimer.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class StudyPlanStatus(str, Enum):
    """Lifecycle of a study plan."""

    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class StudyPlan(Base):
    """A study plan for one subject. Table: ``study_plans``."""

    __tablename__ = "study_plans"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    focus: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[StudyPlanStatus] = mapped_column(
        SQLEnum(StudyPlanStatus, name="study_plan_status", native_enum=True),
        nullable=False,
        default=StudyPlanStatus.ACTIVE,
        server_default=StudyPlanStatus.ACTIVE.name,
        index=True,
    )
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class FlashcardDeck(Base):
    """A named deck of flashcards. Table: ``flashcard_decks``."""

    __tablename__ = "flashcard_decks"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    cards: Mapped[list[Flashcard]] = relationship(
        back_populates="deck", cascade="all, delete-orphan", lazy="selectin"
    )


class Flashcard(Base):
    """One card with its spaced-repetition state. Table: ``flashcards``."""

    __tablename__ = "flashcards"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    deck_id: Mapped[int] = mapped_column(
        ForeignKey("flashcard_decks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    front: Mapped[str] = mapped_column(Text, nullable=False)
    back: Mapped[str] = mapped_column(Text, nullable=False)
    ease: Mapped[float] = mapped_column(Float, nullable=False, default=2.5, server_default="2.5")
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    deck: Mapped[FlashcardDeck] = relationship(back_populates="cards")


class FlashcardReview(Base):
    """Audit of one review event. Table: ``flashcard_reviews``."""

    __tablename__ = "flashcard_reviews"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    flashcard_id: Mapped[int] = mapped_column(
        ForeignKey("flashcards.id", ondelete="CASCADE"), nullable=False, index=True
    )
    grade: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


__all__ = [
    "Flashcard",
    "FlashcardDeck",
    "FlashcardReview",
    "StudyPlan",
    "StudyPlanStatus",
]
