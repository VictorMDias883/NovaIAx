"""
API v1 study endpoints.

All routes live under ``/me/study`` and operate only on the caller's own rows.
Flashcards use an SM-2-style schedule.  Quiz generation is plan-gated behind
``automation`` and mind maps behind ``strong_model``; both return a
``disclaimer``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_router, get_session, require_db_user, require_plan
from app.clients.ai_router import AIRouter
from app.clients.plan_limits import FEATURE_AUTOMATION, FEATURE_STRONG_MODEL
from app.models.study import StudyPlanStatus
from app.schemas.study_schemas import (
    CardCreate,
    CardListResponse,
    CardResponse,
    CardUpdate,
    DeckCreate,
    DeckListResponse,
    DeckResponse,
    DeckUpdate,
    MindMapRequest,
    MindMapResponse,
    QuizRequest,
    QuizResponse,
    ReviewCreate,
    StudyPlanCreate,
    StudyPlanListResponse,
    StudyPlanResponse,
    StudyPlanUpdate,
)
from app.services.study_service import StudyService
from app.services.subscription_service import Entitlement
from app.utils.request import current_user_id

router = APIRouter(prefix="/me/study", tags=["study"])


# ---------------------------------------------------------------------------
# Study plans
# ---------------------------------------------------------------------------
@router.get("/plans", response_model=StudyPlanListResponse)
async def list_plans(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: StudyPlanStatus | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> StudyPlanListResponse:
    rows, total = await StudyService(session).list_plans(
        current_user_id(current_user), page=page, limit=limit, status=status
    )
    return StudyPlanListResponse(
        items=[StudyPlanResponse.model_validate(row) for row in rows], page=page, limit=limit, total=total
    )


@router.post("/plans", response_model=StudyPlanResponse, status_code=201)
async def create_plan(
    payload: StudyPlanCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> StudyPlanResponse:
    plan = await StudyService(session).create_plan(current_user_id(current_user), payload)
    return StudyPlanResponse.model_validate(plan)


@router.get("/plans/{plan_id}", response_model=StudyPlanResponse)
async def get_plan(
    plan_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> StudyPlanResponse:
    plan = await StudyService(session).get_plan(current_user_id(current_user), plan_id)
    return StudyPlanResponse.model_validate(plan)


@router.patch("/plans/{plan_id}", response_model=StudyPlanResponse)
async def update_plan(
    payload: StudyPlanUpdate,
    plan_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> StudyPlanResponse:
    plan = await StudyService(session).update_plan(current_user_id(current_user), plan_id, payload)
    return StudyPlanResponse.model_validate(plan)


@router.delete("/plans/{plan_id}", status_code=204)
async def delete_plan(
    plan_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await StudyService(session).delete_plan(current_user_id(current_user), plan_id)


# ---------------------------------------------------------------------------
# Decks
# ---------------------------------------------------------------------------
@router.get("/decks", response_model=DeckListResponse)
async def list_decks(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> DeckListResponse:
    views, total = await StudyService(session).list_decks(current_user_id(current_user), page=page, limit=limit)
    return DeckListResponse(items=[DeckResponse(**view) for view in views], page=page, limit=limit, total=total)


@router.post("/decks", response_model=DeckResponse, status_code=201)
async def create_deck(
    payload: DeckCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> DeckResponse:
    service = StudyService(session)
    deck = await service.create_deck(current_user_id(current_user), payload)
    return DeckResponse(**await service.get_deck(current_user_id(current_user), deck.id))


@router.get("/decks/{deck_id}", response_model=DeckResponse)
async def get_deck(
    deck_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> DeckResponse:
    return DeckResponse(**await StudyService(session).get_deck(current_user_id(current_user), deck_id))


@router.patch("/decks/{deck_id}", response_model=DeckResponse)
async def update_deck(
    payload: DeckUpdate,
    deck_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> DeckResponse:
    service = StudyService(session)
    deck = await service.update_deck(current_user_id(current_user), deck_id, payload)
    return DeckResponse(**await service.get_deck(current_user_id(current_user), deck.id))


@router.delete("/decks/{deck_id}", status_code=204)
async def delete_deck(
    deck_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await StudyService(session).delete_deck(current_user_id(current_user), deck_id)


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------
@router.get("/decks/{deck_id}/cards", response_model=CardListResponse)
async def list_cards(
    deck_id: int,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    due_only: bool = False,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> CardListResponse:
    rows, total = await StudyService(session).list_cards(
        current_user_id(current_user), deck_id, page=page, limit=limit, due_only=due_only
    )
    return CardListResponse(
        items=[CardResponse.model_validate(row) for row in rows], page=page, limit=limit, total=total
    )


@router.post("/decks/{deck_id}/cards", response_model=CardResponse, status_code=201)
async def add_card(
    payload: CardCreate,
    deck_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> CardResponse:
    card = await StudyService(session).add_card(current_user_id(current_user), deck_id, payload)
    return CardResponse.model_validate(card)


@router.patch("/cards/{card_id}", response_model=CardResponse)
async def update_card(
    payload: CardUpdate,
    card_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> CardResponse:
    card = await StudyService(session).update_card(current_user_id(current_user), card_id, payload)
    return CardResponse.model_validate(card)


@router.delete("/cards/{card_id}", status_code=204)
async def delete_card(
    card_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await StudyService(session).delete_card(current_user_id(current_user), card_id)


@router.post("/cards/{card_id}/reviews", response_model=CardResponse)
async def review_card(
    payload: ReviewCreate,
    card_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> CardResponse:
    card = await StudyService(session).review_card(current_user_id(current_user), card_id, payload.grade)
    return CardResponse.model_validate(card)


# ---------------------------------------------------------------------------
# AI study aids (plan-gated)
# ---------------------------------------------------------------------------
@router.post("/quiz", response_model=QuizResponse)
async def generate_quiz(
    payload: QuizRequest,
    entitlement: Entitlement = Depends(require_plan(FEATURE_AUTOMATION)),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    ai_router: AIRouter = Depends(get_ai_router),
) -> QuizResponse:
    data = await StudyService(session, ai_client=ai_router).generate_quiz(
        current_user_id(current_user),
        plan_tier=entitlement.plan,
        deck_id=payload.deck_id,
        topic=payload.topic,
        count=payload.question_count,
    )
    return QuizResponse(**data)


@router.post("/mind-map", response_model=MindMapResponse)
async def generate_mind_map(
    payload: MindMapRequest,
    entitlement: Entitlement = Depends(require_plan(FEATURE_STRONG_MODEL)),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
    ai_router: AIRouter = Depends(get_ai_router),
) -> MindMapResponse:
    data = await StudyService(session, ai_client=ai_router).generate_mind_map(
        current_user_id(current_user), plan_tier=entitlement.plan, topic=payload.topic
    )
    return MindMapResponse(**data)
