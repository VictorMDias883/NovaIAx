"""
API v1 personal-finance endpoints.

All routes live under ``/me/finance`` and operate only on the caller's own
rows.  Amounts are integer cents.  The summary is a record of the user's own
numbers — it is not financial advice (a ``disclaimer`` accompanies it).
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session, require_db_user
from app.models.finance import BillStatus
from app.schemas.finance_schemas import (
    BillCreate,
    BillListResponse,
    BillResponse,
    BillUpdate,
    BudgetListResponse,
    BudgetResponse,
    BudgetSet,
    ExpenseCreate,
    ExpenseListResponse,
    ExpenseResponse,
    ExpenseUpdate,
    FinanceSummaryResponse,
    IncomeCreate,
    IncomeListResponse,
    IncomeResponse,
    IncomeUpdate,
)
from app.services.finance_service import FinanceService
from app.utils.request import current_user_id

router = APIRouter(prefix="/me/finance", tags=["finance"])


# ---------------------------------------------------------------------------
# Expenses
# ---------------------------------------------------------------------------
@router.get("/expenses", response_model=ExpenseListResponse)
async def list_expenses(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    category: str | None = None,
    from_at: datetime | None = Query(None, alias="from"),
    to_at: datetime | None = Query(None, alias="to"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseListResponse:
    rows, total = await FinanceService(session).list_expenses(
        current_user_id(current_user), page=page, limit=limit, category=category, from_at=from_at, to_at=to_at
    )
    return ExpenseListResponse(
        items=[ExpenseResponse.model_validate(row) for row in rows], page=page, limit=limit, total=total
    )


@router.post("/expenses", response_model=ExpenseResponse, status_code=201)
async def create_expense(
    payload: ExpenseCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseResponse:
    expense = await FinanceService(session).create_expense(current_user_id(current_user), payload)
    return ExpenseResponse.model_validate(expense)


@router.patch("/expenses/{expense_id}", response_model=ExpenseResponse)
async def update_expense(
    payload: ExpenseUpdate,
    expense_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> ExpenseResponse:
    expense = await FinanceService(session).update_expense(current_user_id(current_user), expense_id, payload)
    return ExpenseResponse.model_validate(expense)


@router.delete("/expenses/{expense_id}", status_code=204)
async def delete_expense(
    expense_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await FinanceService(session).delete_expense(current_user_id(current_user), expense_id)


# ---------------------------------------------------------------------------
# Incomes
# ---------------------------------------------------------------------------
@router.get("/incomes", response_model=IncomeListResponse)
async def list_incomes(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    from_at: datetime | None = Query(None, alias="from"),
    to_at: datetime | None = Query(None, alias="to"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> IncomeListResponse:
    rows, total = await FinanceService(session).list_incomes(
        current_user_id(current_user), page=page, limit=limit, from_at=from_at, to_at=to_at
    )
    return IncomeListResponse(
        items=[IncomeResponse.model_validate(row) for row in rows], page=page, limit=limit, total=total
    )


@router.post("/incomes", response_model=IncomeResponse, status_code=201)
async def create_income(
    payload: IncomeCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> IncomeResponse:
    income = await FinanceService(session).create_income(current_user_id(current_user), payload)
    return IncomeResponse.model_validate(income)


@router.patch("/incomes/{income_id}", response_model=IncomeResponse)
async def update_income(
    payload: IncomeUpdate,
    income_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> IncomeResponse:
    income = await FinanceService(session).update_income(current_user_id(current_user), income_id, payload)
    return IncomeResponse.model_validate(income)


@router.delete("/incomes/{income_id}", status_code=204)
async def delete_income(
    income_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await FinanceService(session).delete_income(current_user_id(current_user), income_id)


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------
@router.get("/bills", response_model=BillListResponse)
async def list_bills(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    status: BillStatus | None = None,
    due_from: date | None = None,
    due_to: date | None = None,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> BillListResponse:
    rows, total = await FinanceService(session).list_bills(
        current_user_id(current_user),
        page=page,
        limit=limit,
        status=status,
        due_from=due_from,
        due_to=due_to,
    )
    return BillListResponse(
        items=[BillResponse.model_validate(row) for row in rows], page=page, limit=limit, total=total
    )


@router.post("/bills", response_model=BillResponse, status_code=201)
async def create_bill(
    payload: BillCreate,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> BillResponse:
    bill = await FinanceService(session).create_bill(current_user_id(current_user), payload)
    return BillResponse.model_validate(bill)


@router.patch("/bills/{bill_id}", response_model=BillResponse)
async def update_bill(
    payload: BillUpdate,
    bill_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> BillResponse:
    bill = await FinanceService(session).update_bill(current_user_id(current_user), bill_id, payload)
    return BillResponse.model_validate(bill)


@router.delete("/bills/{bill_id}", status_code=204)
async def delete_bill(
    bill_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await FinanceService(session).delete_bill(current_user_id(current_user), bill_id)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------
@router.get("/budgets", response_model=BudgetListResponse)
async def list_budgets(
    month: str = Query(..., description="Mês no formato AAAA-MM"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> BudgetListResponse:
    rows = await FinanceService(session).list_budgets(current_user_id(current_user), month)
    return BudgetListResponse(month=month, items=[BudgetResponse.model_validate(row) for row in rows])


@router.put("/budgets", response_model=BudgetResponse)
async def set_budget(
    payload: BudgetSet,
    month: str = Query(..., description="Mês no formato AAAA-MM"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> BudgetResponse:
    budget = await FinanceService(session).set_budget(current_user_id(current_user), month, payload)
    return BudgetResponse.model_validate(budget)


@router.delete("/budgets/{budget_id}", status_code=204)
async def delete_budget(
    budget_id: int,
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    await FinanceService(session).delete_budget(current_user_id(current_user), budget_id)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
@router.get("/summary", response_model=FinanceSummaryResponse)
async def finance_summary(
    month: str = Query(..., description="Mês no formato AAAA-MM"),
    current_user: dict = Depends(require_db_user),
    session: AsyncSession = Depends(get_session),
) -> FinanceSummaryResponse:
    data = await FinanceService(session).summary(current_user_id(current_user), month)
    return FinanceSummaryResponse(**data)
