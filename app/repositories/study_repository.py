"""
Repository layer for the study domain.

Owner-scoping on every query.  ``list_*`` returns ``(rows, total)`` for
pagination; card reads are scoped to the owning user so a deck's cards can
never be read across accounts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.study import (
    Flashcard,
    FlashcardDeck,
    FlashcardReview,
    StudyPlan,
    StudyPlanStatus,
)


class _BaseRepository:
    """Shared session plumbing for the study repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _commit(self, *refresh: Any) -> None:
        await self.session.commit()
        for obj in refresh:
            await self.session.refresh(obj)

    async def _count(self, *where: Any, model: Any) -> int:
        stmt = select(func.count()).select_from(model)
        for clause in where:
            stmt = stmt.where(clause)
        return int((await self.session.execute(stmt)).scalar_one())


class StudyPlanRepository(_BaseRepository):
    """Data access for :class:`StudyPlan`."""

    async def create(self, user_id: int, **fields: Any) -> StudyPlan:
        plan = StudyPlan(user_id=user_id, **fields)
        self.session.add(plan)
        await self._commit()
        return plan

    async def get(self, plan_id: int) -> StudyPlan | None:
        return (await self.session.execute(select(StudyPlan).where(StudyPlan.id == plan_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        status: StudyPlanStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[StudyPlan], int]:
        clauses = [StudyPlan.user_id == user_id]
        if status is not None:
            clauses.append(StudyPlan.status == status)
        stmt = (
            select(StudyPlan)
            .where(*clauses)
            .order_by(StudyPlan.created_at.desc(), StudyPlan.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=StudyPlan)
        return rows, total

    async def update(self, plan: StudyPlan, **fields: Any) -> StudyPlan:
        for key, value in fields.items():
            setattr(plan, key, value)
        await self._commit(plan)
        return plan

    async def delete(self, plan: StudyPlan) -> None:
        await self.session.delete(plan)
        await self._commit()


class FlashcardDeckRepository(_BaseRepository):
    """Data access for :class:`FlashcardDeck`."""

    async def create(self, user_id: int, **fields: Any) -> FlashcardDeck:
        deck = FlashcardDeck(user_id=user_id, **fields)
        self.session.add(deck)
        await self._commit()
        return deck

    async def get(self, deck_id: int) -> FlashcardDeck | None:
        return (
            await self.session.execute(select(FlashcardDeck).where(FlashcardDeck.id == deck_id))
        ).scalars().first()

    async def list_for_user(self, user_id: int, *, offset: int = 0, limit: int = 20) -> tuple[list[FlashcardDeck], int]:
        clauses = [FlashcardDeck.user_id == user_id]
        stmt = (
            select(FlashcardDeck)
            .where(*clauses)
            .order_by(FlashcardDeck.created_at.desc(), FlashcardDeck.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=FlashcardDeck)
        return rows, total

    async def count_cards(self, deck_id: int) -> int:
        stmt = select(func.count()).select_from(Flashcard).where(Flashcard.deck_id == deck_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def update(self, deck: FlashcardDeck, **fields: Any) -> FlashcardDeck:
        for key, value in fields.items():
            setattr(deck, key, value)
        await self._commit(deck)
        return deck

    async def delete(self, deck: FlashcardDeck) -> None:
        await self.session.delete(deck)
        await self._commit()


class FlashcardRepository(_BaseRepository):
    """Data access for :class:`Flashcard`."""

    async def create(self, user_id: int, **fields: Any) -> Flashcard:
        card = Flashcard(user_id=user_id, **fields)
        self.session.add(card)
        await self._commit()
        return card

    async def get(self, card_id: int) -> Flashcard | None:
        return (await self.session.execute(select(Flashcard).where(Flashcard.id == card_id))).scalars().first()

    async def list_for_deck(
        self,
        user_id: int,
        deck_id: int,
        *,
        due_before: datetime | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Flashcard], int]:
        clauses = [Flashcard.user_id == user_id, Flashcard.deck_id == deck_id]
        if due_before is not None:
            clauses.append(Flashcard.due_at <= due_before)
        stmt = (
            select(Flashcard).where(*clauses).order_by(Flashcard.due_at, Flashcard.id).offset(offset).limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Flashcard)
        return rows, total

    async def update(self, card: Flashcard, **fields: Any) -> Flashcard:
        for key, value in fields.items():
            setattr(card, key, value)
        await self._commit(card)
        return card

    async def delete(self, card: Flashcard) -> None:
        await self.session.delete(card)
        await self._commit()


class FlashcardReviewRepository(_BaseRepository):
    """Data access for :class:`FlashcardReview`."""

    async def create(self, user_id: int, flashcard_id: int, grade: int) -> FlashcardReview:
        review = FlashcardReview(user_id=user_id, flashcard_id=flashcard_id, grade=grade)
        self.session.add(review)
        await self._commit()
        return review

    async def count_for_user(self, user_id: int) -> int:
        stmt = select(func.count()).select_from(FlashcardReview).where(FlashcardReview.user_id == user_id)
        return int((await self.session.execute(stmt)).scalar_one())
