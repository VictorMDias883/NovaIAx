"""
Service layer for the personal-finance domain.

Business rules:

* every row is owner-scoped;
* amounts are integer cents — the backend only stores and aggregates the user's
  own numbers and **never gives investment, tax, accounting or legal advice**;
  the monthly summary carries an explicit disclaimer;
* a bill marked paid gets a ``paid_at`` timestamp, and a recurring bill rolls
  the next occurrence forward so the series does not die;
* a budget is a per-month, per-category ceiling, upserted on ``(user, month,
  category)``.

    Router → Service → Repository → Database
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.finance import Bill, BillRecurrence, BillStatus, Budget, Expense, Income
from app.repositories.finance_repository import (
    BillRepository,
    BudgetRepository,
    ExpenseRepository,
    IncomeRepository,
)

#: Shown alongside the summary: this is the user's own record, not advice.
FINANCE_DISCLAIMER = (
    "Resumo calculado a partir dos seus próprios registros. Não é consultoria "
    "financeira, contábil, fiscal ou jurídica."
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_month(month: str) -> tuple[int, int]:
    """Parse ``YYYY-MM`` into ``(year, month)`` or raise 422."""
    try:
        year_str, month_str = month.split("-", 1)
        year, month_num = int(year_str), int(month_str)
        if not 1 <= month_num <= 12:
            raise ValueError
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="Mês inválido — use o formato AAAA-MM") from None
    return year, month_num


def _month_bounds(month: str) -> tuple[datetime, datetime, date, date]:
    """Return UTC datetime bounds and date bounds for a ``YYYY-MM`` month."""
    year, month_num = _parse_month(month)
    last_day = calendar.monthrange(year, month_num)[1]
    start = datetime(year, month_num, 1, tzinfo=UTC)
    end = datetime(year, month_num, last_day, 23, 59, 59, 999999, tzinfo=UTC)
    return start, end, date(year, month_num, 1), date(year, month_num, last_day)


def _advance_bill(due: date, recurrence: BillRecurrence) -> date:
    if recurrence is BillRecurrence.MONTHLY:
        month = due.month + 1
        year = due.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        day = min(due.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)
    if recurrence is BillRecurrence.YEARLY:
        try:
            return due.replace(year=due.year + 1)
        except ValueError:
            return due.replace(year=due.year + 1, day=28)
    return due


class FinanceService:
    """CRUD and the monthly summary for the caller's own finances."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.expenses = ExpenseRepository(session)
        self.incomes = IncomeRepository(session)
        self.bills = BillRepository(session)
        self.budgets = BudgetRepository(session)

    # ------------------------------------------------------------------
    # Expenses
    # ------------------------------------------------------------------
    async def create_expense(self, user_id: int, payload) -> Expense:  # noqa: ANN001
        return await self.expenses.create(
            user_id,
            amount_cents=payload.amount_cents,
            category=payload.category,
            description=payload.description,
            occurred_at=_as_utc(payload.occurred_at),
        )

    async def list_expenses(
        self,
        user_id: int,
        *,
        page: int,
        limit: int,
        category: str | None = None,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> tuple[list[Expense], int]:
        return await self.expenses.list_for_user(
            user_id,
            category=category,
            from_at=_as_utc(from_at),
            to_at=_as_utc(to_at),
            offset=(page - 1) * limit,
            limit=limit,
        )

    async def _owned_expense(self, user_id: int, expense_id: int) -> Expense:
        expense = await self.expenses.get(expense_id)
        if expense is None or expense.user_id != user_id:
            raise HTTPException(status_code=404, detail="Despesa não encontrada")
        return expense

    async def update_expense(self, user_id: int, expense_id: int, payload) -> Expense:  # noqa: ANN001
        expense = await self._owned_expense(user_id, expense_id)
        fields = payload.model_dump(exclude_unset=True)
        if fields.get("occurred_at") is not None:
            fields["occurred_at"] = _as_utc(fields["occurred_at"])
        return await self.expenses.update(expense, **fields)

    async def delete_expense(self, user_id: int, expense_id: int) -> None:
        await self.expenses.delete(await self._owned_expense(user_id, expense_id))

    # ------------------------------------------------------------------
    # Incomes
    # ------------------------------------------------------------------
    async def create_income(self, user_id: int, payload) -> Income:  # noqa: ANN001
        return await self.incomes.create(
            user_id,
            amount_cents=payload.amount_cents,
            source=payload.source,
            description=payload.description,
            occurred_at=_as_utc(payload.occurred_at),
        )

    async def list_incomes(
        self,
        user_id: int,
        *,
        page: int,
        limit: int,
        from_at: datetime | None = None,
        to_at: datetime | None = None,
    ) -> tuple[list[Income], int]:
        return await self.incomes.list_for_user(
            user_id, from_at=_as_utc(from_at), to_at=_as_utc(to_at), offset=(page - 1) * limit, limit=limit
        )

    async def _owned_income(self, user_id: int, income_id: int) -> Income:
        income = await self.incomes.get(income_id)
        if income is None or income.user_id != user_id:
            raise HTTPException(status_code=404, detail="Receita não encontrada")
        return income

    async def update_income(self, user_id: int, income_id: int, payload) -> Income:  # noqa: ANN001
        income = await self._owned_income(user_id, income_id)
        fields = payload.model_dump(exclude_unset=True)
        if fields.get("occurred_at") is not None:
            fields["occurred_at"] = _as_utc(fields["occurred_at"])
        return await self.incomes.update(income, **fields)

    async def delete_income(self, user_id: int, income_id: int) -> None:
        await self.incomes.delete(await self._owned_income(user_id, income_id))

    # ------------------------------------------------------------------
    # Bills
    # ------------------------------------------------------------------
    async def create_bill(self, user_id: int, payload) -> Bill:  # noqa: ANN001
        return await self.bills.create(
            user_id,
            name=payload.name,
            amount_cents=payload.amount_cents,
            due_date=payload.due_date,
            status=BillStatus.PENDING,
            recurrence=payload.recurrence,
        )

    async def list_bills(
        self,
        user_id: int,
        *,
        page: int,
        limit: int,
        status: BillStatus | None = None,
        due_from: date | None = None,
        due_to: date | None = None,
    ) -> tuple[list[Bill], int]:
        return await self.bills.list_for_user(
            user_id, status=status, due_from=due_from, due_to=due_to, offset=(page - 1) * limit, limit=limit
        )

    async def _owned_bill(self, user_id: int, bill_id: int) -> Bill:
        bill = await self.bills.get(bill_id)
        if bill is None or bill.user_id != user_id:
            raise HTTPException(status_code=404, detail="Conta não encontrada")
        return bill

    async def update_bill(self, user_id: int, bill_id: int, payload) -> Bill:  # noqa: ANN001
        bill = await self._owned_bill(user_id, bill_id)
        fields = payload.model_dump(exclude_unset=True)
        new_status = fields.get("status")
        if new_status is BillStatus.PAID and bill.status is not BillStatus.PAID:
            fields["paid_at"] = datetime.now(UTC)
        updated = await self.bills.update(bill, **fields)
        if new_status is BillStatus.PAID and updated.recurrence is not BillRecurrence.NONE:
            await self.bills.create(
                user_id,
                name=updated.name,
                amount_cents=updated.amount_cents,
                due_date=_advance_bill(updated.due_date, updated.recurrence),
                status=BillStatus.PENDING,
                recurrence=updated.recurrence,
            )
        return updated

    async def delete_bill(self, user_id: int, bill_id: int) -> None:
        await self.bills.delete(await self._owned_bill(user_id, bill_id))

    # ------------------------------------------------------------------
    # Budgets
    # ------------------------------------------------------------------
    async def set_budget(self, user_id: int, month: str, payload) -> Budget:  # noqa: ANN001
        _parse_month(month)
        return await self.budgets.upsert(
            user_id, month=month, category=payload.category, amount_cents=payload.amount_cents
        )

    async def list_budgets(self, user_id: int, month: str) -> list[Budget]:
        _parse_month(month)
        return await self.budgets.list_for_month(user_id, month)

    async def delete_budget(self, user_id: int, budget_id: int) -> None:
        budget = await self.budgets.get(budget_id)
        if budget is None or budget.user_id != user_id:
            raise HTTPException(status_code=404, detail="Orçamento não encontrado")
        await self.budgets.delete(budget)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    async def summary(self, user_id: int, month: str) -> dict:
        start, end, date_start, date_end = _month_bounds(month)
        income_total = await self.incomes.total_between(user_id, start, end)
        expense_total = await self.expenses.total_between(user_id, start, end)
        by_category = await self.expenses.sum_by_category(user_id, start, end)
        bill_counts = await self.bills.count_by_status(user_id, date_start, date_end)
        due_total = await self.bills.sum_due_between(user_id, date_start, date_end, status=BillStatus.PENDING)

        budgets = await self.budgets.list_for_month(user_id, month)
        budget_status = [
            {
                "category": budget.category,
                "limit_cents": budget.amount_cents,
                "spent_cents": by_category.get(budget.category, 0),
                "remaining_cents": budget.amount_cents - by_category.get(budget.category, 0),
                "over": by_category.get(budget.category, 0) > budget.amount_cents,
            }
            for budget in budgets
        ]

        return {
            "month": month,
            "income_cents": income_total,
            "expense_cents": expense_total,
            "balance_cents": income_total - expense_total,
            "expenses_by_category": by_category,
            "bills": {
                "pending": bill_counts.get(BillStatus.PENDING.value, 0),
                "paid": bill_counts.get(BillStatus.PAID.value, 0),
                "overdue": bill_counts.get(BillStatus.OVERDUE.value, 0),
                "due_cents": due_total,
            },
            "budgets": budget_status,
            "disclaimer": FINANCE_DISCLAIMER,
        }
