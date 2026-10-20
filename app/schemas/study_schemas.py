"""Pydantic schemas for the study endpoints."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.study import StudyPlanStatus


class StudyPlanCreate(BaseModel):
    """Request body for creating a study plan."""

    subject: str = Field(min_length=1, max_length=200)
    focus: str | None = Field(default=None, max_length=5000)
    target_date: date | None = None


class StudyPlanUpdate(BaseModel):
    """Request body for patching a study plan (all fields optional)."""

    subject: str | None = Field(default=None, min_length=1, max_length=200)
    focus: str | None = Field(default=None, max_length=5000)
    status: StudyPlanStatus | None = None
    target_date: date | None = None


class StudyPlanResponse(BaseModel):
    """Response body for a study plan."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    focus: str | None = None
    status: StudyPlanStatus
    target_date: date | None = None
    created_at: datetime
    updated_at: datetime


class StudyPlanListResponse(BaseModel):
    """Paginated study-plan list."""

    items: list[StudyPlanResponse]
    page: int
    limit: int
    total: int


class DeckCreate(BaseModel):
    """Request body for creating a flashcard deck."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)


class DeckUpdate(BaseModel):
    """Request body for patching a deck (all fields optional)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)


class DeckResponse(BaseModel):
    """Response body for a deck, including its card count."""

    id: int
    name: str
    description: str | None = None
    card_count: int
    created_at: datetime
    updated_at: datetime


class DeckListResponse(BaseModel):
    """Paginated deck list."""

    items: list[DeckResponse]
    page: int
    limit: int
    total: int


class CardCreate(BaseModel):
    """Request body for adding a card to a deck."""

    front: str = Field(min_length=1, max_length=5000)
    back: str = Field(min_length=1, max_length=5000)


class CardUpdate(BaseModel):
    """Request body for patching a card (all fields optional)."""

    front: str | None = Field(default=None, min_length=1, max_length=5000)
    back: str | None = Field(default=None, min_length=1, max_length=5000)


class CardResponse(BaseModel):
    """Response body for a single flashcard with its SM-2 state."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    deck_id: int
    front: str
    back: str
    ease: float
    interval_days: int
    repetitions: int
    due_at: datetime
    last_reviewed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CardListResponse(BaseModel):
    """Paginated card list."""

    items: list[CardResponse]
    page: int
    limit: int
    total: int


class ReviewCreate(BaseModel):
    """Request body for reviewing a card.

    ``grade`` follows the SM-2 quality scale: ``0``–``2`` is a lapse,
    ``3``–``5`` is a successful recall (higher = easier).
    """

    grade: int = Field(ge=0, le=5)


class QuizRequest(BaseModel):
    """Request body to generate a quiz for a deck or a free topic."""

    deck_id: int | None = None
    topic: str | None = Field(default=None, max_length=500)
    question_count: int = Field(default=5, ge=1, le=10)


class QuizQuestion(BaseModel):
    """One generated multiple-choice question."""

    question: str
    options: list[str]
    answer_index: int
    explanation: str | None = None


class QuizResponse(BaseModel):
    """Generated quiz with a disclaimer."""

    questions: list[QuizQuestion]
    disclaimer: str


class MindMapRequest(BaseModel):
    """Request body to generate a mind map for a topic."""

    topic: str = Field(min_length=1, max_length=500)


class MindMapNode(BaseModel):
    """A node in the generated mind map."""

    label: str
    children: list[MindMapNode] = Field(default_factory=list)


class MindMapResponse(BaseModel):
    """Generated mind map with a disclaimer."""

    root: MindMapNode
    disclaimer: str
