"""
Repository layer for the personal-finance domain.

Owner-scoping is enforced on every query.  Amounts are integer cents.  Each
repository exposes the small set of reads the service's monthly summary needs
(group-by-category sums, totals, status counts) so the summary runs in a few
queries rather than loading every row.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finance import Bill, BillStatus, Budget, Expense, Income


class _BaseRepository:
    """Shared session plumbing for the finance repositories."""

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


class ExpenseRepository(_BaseRepository):
    """Data access for :class:`Expense`."""

    async def create(self, user_id: int, **fields: Any) -> Expense:
        expense = Expense(user_id=user_id, **fields)
        self.session.add(expense)
        await self._commit()
        return expense

    async def get(self, expense_id: int) -> Expense | None:
        return (await self.session.execute(select(Expense).where(Expense.id == expense_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        category: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Expense], int]:
        clauses = [Expense.user_id == user_id]
        if category:
            clauses.append(Expense.category == category)
        if from_at is not None:
            clauses.append(Expense.occurred_at >= from_at)
        if to_at is not None:
            clauses.append(Expense.occurred_at <= to_at)
        stmt = (
            select(Expense)
            .where(*clauses)
            .order_by(Expense.occurred_at.desc(), Expense.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Expense)
        return rows, total

    async def sum_by_category(self, user_id: int, start: datetime, end: datetime) -> dict[str, int]:
        stmt = (
            select(Expense.category, func.coalesce(func.sum(Expense.amount_cents), 0))
            .where(Expense.user_id == user_id, Expense.occurred_at >= start, Expense.occurred_at <= end)
            .group_by(Expense.category)
        )
        rows = (await self.session.execute(stmt)).all()
        return {str(category): int(total) for category, total in rows}

    async def total_between(self, user_id: int, start: datetime, end: datetime) -> int:
        stmt = select(func.coalesce(func.sum(Expense.amount_cents), 0)).where(
            Expense.user_id == user_id, Expense.occurred_at >= start, Expense.occurred_at <= end
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def update(self, expense: Expense, **fields: Any) -> Expense:
        for key, value in fields.items():
            setattr(expense, key, value)
        await self._commit(expense)
        return expense

    async def delete(self, expense: Expense) -> None:
        await self.session.delete(expense)
        await self._commit()


class IncomeRepository(_BaseRepository):
    """Data access for :class:`Income`."""

    async def create(self, user_id: int, **fields: Any) -> Income:
        income = Income(user_id=user_id, **fields)
        self.session.add(income)
        await self._commit()
        return income

    async def get(self, income_id: int) -> Income | None:
        return (await self.session.execute(select(Income).where(Income.id == income_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Income], int]:
        clauses = [Income.user_id == user_id]
        if from_at is not None:
            clauses.append(Income.occurred_at >= from_at)
        if to_at is not None:
            clauses.append(Income.occurred_at <= to_at)
        stmt = (
            select(Income)
            .where(*clauses)
            .order_by(Income.occurred_at.desc(), Income.id.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Income)
        return rows, total

    async def total_between(self, user_id: int, start: datetime, end: datetime) -> int:
        stmt = select(func.coalesce(func.sum(Income.amount_cents), 0)).where(
            Income.user_id == user_id, Income.occurred_at >= start, Income.occurred_at <= end
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def update(self, income: Income, **fields: Any) -> Income:
        for key, value in fields.items():
            setattr(income, key, value)
        await self._commit(income)
        return income

    async def delete(self, income: Income) -> None:
        await self.session.delete(income)
        await self._commit()


class BillRepository(_BaseRepository):
    """Data access for :class:`Bill`."""

    async def create(self, user_id: int, **fields: Any) -> Bill:
        bill = Bill(user_id=user_id, **fields)
        self.session.add(bill)
        await self._commit()
        return bill

    async def get(self, bill_id: int) -> Bill | None:
        return (await self.session.execute(select(Bill).where(Bill.id == bill_id))).scalars().first()

    async def list_for_user(
        self,
        user_id: int,
        *,
        status: BillStatus | None = None,
        due_from: date | None = None,
        due_to: date | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Bill], int]:
        clauses = [Bill.user_id == user_id]
        if status is not None:
            clauses.append(Bill.status == status)
        if due_from is not None:
            clauses.append(Bill.due_date >= due_from)
        if due_to is not None:
            clauses.append(Bill.due_date <= due_to)
        stmt = select(Bill).where(*clauses).order_by(Bill.due_date, Bill.id).offset(offset).limit(limit)
        rows = list((await self.session.execute(stmt)).scalars().all())
        total = await self._count(*clauses, model=Bill)
        return rows, total

    async def count_by_status(self, user_id: int, start: date, end: date) -> dict[str, int]:
        stmt = (
            select(Bill.status, func.count())
            .where(Bill.user_id == user_id, Bill.due_date >= start, Bill.due_date <= end)
            .group_by(Bill.status)
        )
        rows = (await self.session.execute(stmt)).all()
        return {str(status.value if hasattr(status, "value") else status): int(count) for status, count in rows}

    async def sum_due_between(self, user_id: int, start: date, end: date, *, status: BillStatus | None = None) -> int:
        clauses = [Bill.user_id == user_id, Bill.due_date >= start, Bill.due_date <= end]
        if status is not None:
            clauses.append(Bill.status == status)
        stmt = select(func.coalesce(func.sum(Bill.amount_cents), 0)).where(*clauses)
        return int((await self.session.execute(stmt)).scalar_one())

    async def update(self, bill: Bill, **fields: Any) -> Bill:
        for key, value in fields.items():
            setattr(bill, key, value)
        await self._commit(bill)
        return bill

    async def delete(self, bill: Bill) -> None:
        await self.session.delete(bill)
        await self._commit()


class BudgetRepository(_BaseRepository):
    """Data access for :class:`Budget`."""

    async def upsert(self, user_id: int, *, month: str, category: str, amount_cents: int) -> Budget:
        existing = (
            await self.session.execute(
                select(Budget).where(
                    Budget.user_id == user_id, Budget.month == month, Budget.category == category
                )
            )
        ).scalars().first()
        if existing is not None:
            existing.amount_cents = amount_cents
            await self._commit(existing)
            return existing
        budget = Budget(user_id=user_id, month=month, category=category, amount_cents=amount_cents)
        self.session.add(budget)
        await self._commit()
        return budget

    async def get(self, budget_id: int) -> Budget | None:
        return (await self.session.execute(select(Budget).where(Budget.id == budget_id))).scalars().first()

    async def list_for_month(self, user_id: int, month: str) -> list[Budget]:
        stmt = select(Budget).where(Budget.user_id == user_id, Budget.month == month).order_by(Budget.category)
        return list((await self.session.execute(stmt)).scalars().all())

    async def delete(self, budget: Budget) -> None:
        await self.session.delete(budget)
        await self._commit()
