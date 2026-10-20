"""
Service layer for the study domain.

Highlights:

* study plans and flashcard decks are owner-scoped;
* flashcards use an **SM-2-style** spaced-repetition schedule — the next
  ``due_at`` is derived from the review grade;
* quiz and mind-map generation call the AI, are plan-gated at the router, and
  always return a ``disclaimer``; generated content is never persisted as
  authoritative material and never presented as a medical/legal/financial
  source.

    Router → Service → Repository → Database
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_router import AIRouter, run_ai_request
from app.clients.ai_types import AIRequest, AITaskType, PlanTier
from app.models.study import Flashcard, FlashcardDeck, StudyPlan
from app.repositories.study_repository import (
    FlashcardDeckRepository,
    FlashcardRepository,
    FlashcardReviewRepository,
    StudyPlanRepository,
)
from app.utils.json_extraction import extract_json

#: Shown alongside generated study material.
STUDY_DISCLAIMER = (
    "Conteúdo gerado por IA para apoiar seus estudos. Revise e confira com "
    "fontes confiáveis antes de usar."
)

_MIN_EASE = 1.3

_QUIZ_SYSTEM_PROMPT = (
    "Você gera quizzes de estudo. Responda EXCLUSIVAMENTE em JSON no formato "
    '{"questions": [{"question": "string", "options": ["a", "b", "c", "d"], '
    '"answer_index": 0, "explanation": "string ou null"}]}. '
    "Cada questão deve ter entre 2 e 5 opções e answer_index válido. Não inclua texto fora do JSON."
)

_MIND_MAP_SYSTEM_PROMPT = (
    "Você monta mapas mentais. Responda EXCLUSIVAMENTE em JSON no formato "
    '{"root": {"label": "tema", "children": [{"label": "ramo", "children": []}]}}. '
    "Máximo de 3 níveis de profundidade e 8 filhos por nó. Não inclua texto fora do JSON."
)


def _sm2(card: Flashcard, grade: int) -> dict[str, object]:
    """Return the SM-2 field updates for ``card`` given a review ``grade``."""
    ease = card.ease
    repetitions = card.repetitions
    interval = card.interval_days

    if grade >= 3:
        if repetitions == 0:
            interval = 1
        elif repetitions == 1:
            interval = 6
        else:
            interval = round(interval * ease) or 1
        repetitions += 1
    else:
        repetitions = 0
        interval = 1

    ease = ease + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    ease = max(_MIN_EASE, ease)

    now = datetime.now(UTC)
    return {
        "ease": round(ease, 4),
        "repetitions": repetitions,
        "interval_days": interval,
        "due_at": now + timedelta(days=interval),
        "last_reviewed_at": now,
    }


class StudyService:
    """CRUD, spaced repetition and AI study aids for one user."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        ai_client: AIRouter | None = None,
    ) -> None:
        self.session = session
        self.ai_client = ai_client
        self.plans = StudyPlanRepository(session)
        self.decks = FlashcardDeckRepository(session)
        self.cards = FlashcardRepository(session)
        self.reviews = FlashcardReviewRepository(session)

    # ------------------------------------------------------------------
    # Study plans
    # ------------------------------------------------------------------
    async def create_plan(self, user_id: int, payload) -> StudyPlan:  # noqa: ANN001
        return await self.plans.create(
            user_id, subject=payload.subject, focus=payload.focus, target_date=payload.target_date
        )

    async def list_plans(
        self, user_id: int, *, page: int, limit: int, status=None
    ) -> tuple[list[StudyPlan], int]:
        return await self.plans.list_for_user(user_id, status=status, offset=(page - 1) * limit, limit=limit)

    async def _owned_plan(self, user_id: int, plan_id: int) -> StudyPlan:
        plan = await self.plans.get(plan_id)
        if plan is None or plan.user_id != user_id:
            raise HTTPException(status_code=404, detail="Plano de estudo não encontrado")
        return plan

    async def get_plan(self, user_id: int, plan_id: int) -> StudyPlan:
        return await self._owned_plan(user_id, plan_id)

    async def update_plan(self, user_id: int, plan_id: int, payload) -> StudyPlan:  # noqa: ANN001
        plan = await self._owned_plan(user_id, plan_id)
        return await self.plans.update(plan, **payload.model_dump(exclude_unset=True))

    async def delete_plan(self, user_id: int, plan_id: int) -> None:
        await self.plans.delete(await self._owned_plan(user_id, plan_id))

    # ------------------------------------------------------------------
    # Decks
    # ------------------------------------------------------------------
    async def create_deck(self, user_id: int, payload) -> FlashcardDeck:
        return await self.decks.create(user_id, name=payload.name, description=payload.description)

    async def list_decks(self, user_id: int, *, page: int, limit: int):
        rows, total = await self.decks.list_for_user(user_id, offset=(page - 1) * limit, limit=limit)
        views = [
            {
                "id": deck.id,
                "name": deck.name,
                "description": deck.description,
                "card_count": await self.decks.count_cards(deck.id),
                "created_at": deck.created_at,
                "updated_at": deck.updated_at,
            }
            for deck in rows
        ]
        return views, total

    async def _owned_deck(self, user_id: int, deck_id: int) -> FlashcardDeck:
        deck = await self.decks.get(deck_id)
        if deck is None or deck.user_id != user_id:
            raise HTTPException(status_code=404, detail="Baralho não encontrado")
        return deck

    async def get_deck(self, user_id: int, deck_id: int) -> dict:
        deck = await self._owned_deck(user_id, deck_id)
        return {
            "id": deck.id,
            "name": deck.name,
            "description": deck.description,
            "card_count": await self.decks.count_cards(deck.id),
            "created_at": deck.created_at,
            "updated_at": deck.updated_at,
        }

    async def update_deck(self, user_id: int, deck_id: int, payload) -> FlashcardDeck:  # noqa: ANN001
        deck = await self._owned_deck(user_id, deck_id)
        return await self.decks.update(deck, **payload.model_dump(exclude_unset=True))

    async def delete_deck(self, user_id: int, deck_id: int) -> None:
        await self.decks.delete(await self._owned_deck(user_id, deck_id))

    # ------------------------------------------------------------------
    # Cards
    # ------------------------------------------------------------------
    async def add_card(self, user_id: int, deck_id: int, payload) -> Flashcard:
        await self._owned_deck(user_id, deck_id)
        return await self.cards.create(user_id, deck_id=deck_id, front=payload.front, back=payload.back, due_at=datetime.now(UTC))

    async def list_cards(
        self, user_id: int, deck_id: int, *, page: int, limit: int, due_only: bool = False
    ) -> tuple[list[Flashcard], int]:
        await self._owned_deck(user_id, deck_id)
        due_before = datetime.now(UTC) if due_only else None
        return await self.cards.list_for_deck(
            user_id, deck_id, due_before=due_before, offset=(page - 1) * limit, limit=limit
        )

    async def _owned_card(self, user_id: int, card_id: int) -> Flashcard:
        card = await self.cards.get(card_id)
        if card is None or card.user_id != user_id:
            raise HTTPException(status_code=404, detail="Cartão não encontrado")
        return card

    async def update_card(self, user_id: int, card_id: int, payload) -> Flashcard:  # noqa: ANN001
        card = await self._owned_card(user_id, card_id)
        return await self.cards.update(card, **payload.model_dump(exclude_unset=True))

    async def delete_card(self, user_id: int, card_id: int) -> None:
        await self.cards.delete(await self._owned_card(user_id, card_id))

    async def review_card(self, user_id: int, card_id: int, grade: int) -> Flashcard:
        card = await self._owned_card(user_id, card_id)
        await self.reviews.create(user_id, card.id, grade)
        return await self.cards.update(card, **_sm2(card, grade))

    # ------------------------------------------------------------------
    # AI: quiz and mind map (plan-gated at the router)
    # ------------------------------------------------------------------
    async def generate_quiz(self, user_id: int, *, plan_tier: PlanTier, deck_id: int | None, topic: str | None, count: int) -> dict:
        if self.ai_client is None:
            raise HTTPException(status_code=503, detail="Serviço de IA indisponível")
        context = topic
        if deck_id is not None:
            await self._owned_deck(user_id, deck_id)
            cards, _ = await self.cards.list_for_deck(user_id, deck_id, offset=0, limit=30)
            context = "; ".join(f"{card.front} = {card.back}" for card in cards) or topic
        if not context:
            raise HTTPException(status_code=422, detail="Informe um tema ou um baralho com cartões")
        assistant = await run_ai_request(
            self.ai_client,
            AIRequest(
                system_prompt=_QUIZ_SYSTEM_PROMPT,
                user_message=f"Crie {count} questões sobre: {context}",
                task_type=AITaskType.STRICT_JSON,
                plan_tier=plan_tier,
                user_id=user_id,
            ),
        )
        parsed = extract_json(assistant.text, context="Study quiz")
        return {"questions": self._parse_questions(parsed), "disclaimer": STUDY_DISCLAIMER}

    @staticmethod
    def _parse_questions(parsed: object) -> list[dict]:
        if not isinstance(parsed, dict):
            return []
        raw = parsed.get("questions")
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for entry in raw[:10]:
            if not isinstance(entry, dict):
                continue
            question = str(entry.get("question", "")).strip()
            options = entry.get("options")
            if not question or not isinstance(options, list) or len(options) < 2:
                continue
            clean_options = [str(option)[:500] for option in options[:5]]
            try:
                answer_index = int(entry.get("answer_index", 0))
            except (TypeError, ValueError):
                answer_index = 0
            if not 0 <= answer_index < len(clean_options):
                answer_index = 0
            explanation = entry.get("explanation")
            out.append(
                {
                    "question": question[:2000],
                    "options": clean_options,
                    "answer_index": answer_index,
                    "explanation": str(explanation)[:2000] if explanation else None,
                }
            )
        return out

    async def generate_mind_map(self, user_id: int, *, plan_tier: PlanTier, topic: str) -> dict:
        if self.ai_client is None:
            raise HTTPException(status_code=503, detail="Serviço de IA indisponível")
        assistant = await run_ai_request(
            self.ai_client,
            AIRequest(
                system_prompt=_MIND_MAP_SYSTEM_PROMPT,
                user_message=f"Monte um mapa mental sobre: {topic}",
                task_type=AITaskType.STRICT_JSON,
                plan_tier=plan_tier,
                user_id=user_id,
            ),
        )
        parsed = extract_json(assistant.text, context="Mind map")
        root = self._parse_node(parsed.get("root") if isinstance(parsed, dict) else None, depth=0)
        if root is None:
            root = {"label": topic[:200], "children": []}
        return {"root": root, "disclaimer": STUDY_DISCLAIMER}

    @classmethod
    def _parse_node(cls, node: object, *, depth: int) -> dict | None:
        if not isinstance(node, dict):
            return None
        label = str(node.get("label", "")).strip()
        if not label:
            return None
        children: list[dict] = []
        if depth < 2:
            raw_children = node.get("children")
            if isinstance(raw_children, list):
                for child in raw_children[:8]:
                    parsed_child = cls._parse_node(child, depth=depth + 1)
                    if parsed_child is not None:
                        children.append(parsed_child)
        return {"label": label[:200], "children": children}
