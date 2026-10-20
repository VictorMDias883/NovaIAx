"""Pydantic schemas for the personal-finance endpoints.

All amounts are **integer cents** (never floats).  Clients divide by 100 to
display.  ``month`` is a ``YYYY-MM`` string.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.finance import BillRecurrence, BillStatus


class ExpenseCreate(BaseModel):
    """Request body for creating an expense."""

    amount_cents: int = Field(gt=0)
    category: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=5000)
    occurred_at: datetime


class ExpenseUpdate(BaseModel):
    """Request body for patching an expense (all fields optional)."""

    amount_cents: int | None = Field(default=None, gt=0)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=5000)
    occurred_at: datetime | None = None


class ExpenseResponse(BaseModel):
    """Response body for a single expense."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    amount_cents: int
    category: str
    description: str | None = None
    occurred_at: datetime
    created_at: datetime


class ExpenseListResponse(BaseModel):
    """Paginated expense list."""

    items: list[ExpenseResponse]
    page: int
    limit: int
    total: int


class IncomeCreate(BaseModel):
    """Request body for creating an income."""

    amount_cents: int = Field(gt=0)
    source: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=5000)
    occurred_at: datetime


class IncomeUpdate(BaseModel):
    """Request body for patching an income (all fields optional)."""

    amount_cents: int | None = Field(default=None, gt=0)
    source: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=5000)
    occurred_at: datetime | None = None


class IncomeResponse(BaseModel):
    """Response body for a single income."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    amount_cents: int
    source: str
    description: str | None = None
    occurred_at: datetime
    created_at: datetime


class IncomeListResponse(BaseModel):
    """Paginated income list."""

    items: list[IncomeResponse]
    page: int
    limit: int
    total: int


class BillCreate(BaseModel):
    """Request body for creating a bill."""

    name: str = Field(min_length=1, max_length=200)
    amount_cents: int = Field(gt=0)
    due_date: date
    recurrence: BillRecurrence = BillRecurrence.NONE


class BillUpdate(BaseModel):
    """Request body for patching a bill (all fields optional)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    amount_cents: int | None = Field(default=None, gt=0)
    due_date: date | None = None
    status: BillStatus | None = None
    recurrence: BillRecurrence | None = None


class BillResponse(BaseModel):
    """Response body for a single bill."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    amount_cents: int
    due_date: date
    status: BillStatus
    recurrence: BillRecurrence
    paid_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BillListResponse(BaseModel):
    """Paginated bill list."""

    items: list[BillResponse]
    page: int
    limit: int
    total: int


class BudgetSet(BaseModel):
    """Request body to set (upsert) a monthly category budget."""

    category: str = Field(min_length=1, max_length=80)
    amount_cents: int = Field(gt=0)


class BudgetResponse(BaseModel):
    """Response body for a single budget."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    month: str
    category: str
    amount_cents: int
    created_at: datetime
    updated_at: datetime


class BudgetListResponse(BaseModel):
    """List of the month's budgets."""

    month: str
    items: list[BudgetResponse]


class BudgetStatusResponse(BaseModel):
    """One budget compared against the month's spending in its category."""

    category: str
    limit_cents: int
    spent_cents: int
    remaining_cents: int
    over: bool


class BillsSummaryResponse(BaseModel):
    """Aggregated bill figures for the month."""

    pending: int
    paid: int
    overdue: int
    due_cents: int


class FinanceSummaryResponse(BaseModel):
    """Monthly financial overview for the caller.

    The ``disclaimer`` states plainly that this is a record of the user's own
    numbers, not financial advice.
    """

    month: str
    income_cents: int
    expense_cents: int
    balance_cents: int
    expenses_by_category: dict[str, int]
    bills: BillsSummaryResponse
    budgets: list[BudgetStatusResponse]
    disclaimer: str
