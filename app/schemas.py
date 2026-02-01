from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


# --- Enums ---

class CategoryType(str, Enum):
    income = "income"
    expense = "expense"


class TransactionType(str, Enum):
    regular = "regular"
    salary = "salary"
    salary_allocation = "salary_allocation"
    contribution = "contribution"
    withdrawal = "withdrawal"
    budget_expense = "budget_expense"
    budget_transfer = "budget_transfer"


class BillFrequency(str, Enum):
    twenty_eight_days = "28_days"
    monthly = "monthly"
    quarterly = "quarterly"
    yearly = "yearly"


class BillsAllocationMethod(str, Enum):
    recommended = "recommended"
    fixed = "fixed"


# --- User Schemas ---

class UserCreate(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)
    email: str | None = None


class UserUpdate(BaseModel):
    username: str | None = None
    password: str | None = Field(None, min_length=8)
    email: str | None = None


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None = None
    created_at: datetime
    updated_at: datetime


# --- Category Schemas ---

class CategoryCreate(BaseModel):
    name: str = Field(..., min_length=1)
    type: CategoryType
    color: str = Field(..., pattern=r"^#[0-9a-fA-F]{6}$")
    is_budget_category: bool = False


class CategoryUpdate(BaseModel):
    name: str | None = None
    type: CategoryType | None = None
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    is_budget_category: bool | None = None


class CategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: CategoryType
    color: str
    is_budget_category: bool
    is_deleted: bool


# --- Transaction Schemas ---

class TransactionCreate(BaseModel):
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: str | None = None
    amount: Decimal = Field(..., gt=0)
    category_id: int
    type: CategoryType
    transaction_type: TransactionType = TransactionType.regular
    sinking_fund_id: int | None = None
    recurring_bill_id: int | None = None
    budget_id: int | None = None
    is_paid: bool = True


class TransactionUpdate(BaseModel):
    date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: str | None = None
    amount: Decimal | None = Field(None, gt=0)
    category_id: int | None = None
    type: CategoryType | None = None
    transaction_type: TransactionType | None = None
    sinking_fund_id: int | None = None
    recurring_bill_id: int | None = None
    budget_id: int | None = None
    is_paid: bool | None = None


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    date: str
    description: str | None = None
    amount: Decimal
    category_id: int
    type: CategoryType
    transaction_type: TransactionType
    sinking_fund_id: int | None = None
    recurring_bill_id: int | None = None
    budget_id: int | None = None
    is_paid: bool
    created_at: datetime


# --- Budget Schemas ---

class BudgetCreate(BaseModel):
    category_id: int
    month: int = Field(..., ge=1, le=12)
    year: int = Field(..., ge=2000)
    allocated_amount: Decimal = Field(..., ge=0)
    spent_amount: Decimal = Field(default=Decimal("0"), ge=0)
    fund_balance: Decimal = Field(default=Decimal("0"), ge=0)


class BudgetUpdate(BaseModel):
    allocated_amount: Decimal | None = Field(None, ge=0)
    spent_amount: Decimal | None = Field(None, ge=0)
    fund_balance: Decimal | None = None


class BudgetResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category_id: int
    month: int
    year: int
    allocated_amount: Decimal
    spent_amount: Decimal
    fund_balance: Decimal
    created_at: datetime
    updated_at: datetime


# --- Sinking Fund Schemas ---

class SinkingFundCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    monthly_allocation: Decimal = Field(..., ge=0)
    current_balance: Decimal = Field(default=Decimal("0"))
    color: str = Field(..., pattern=r"^#[0-9a-fA-F]{6}$")


class SinkingFundUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    monthly_allocation: Decimal | None = Field(None, ge=0)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")


class SinkingFundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    monthly_allocation: Decimal
    current_balance: Decimal
    color: str
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


# --- Recurring Bill Schemas ---

class RecurringBillCreate(BaseModel):
    name: str = Field(..., min_length=1)
    amount: Decimal = Field(..., gt=0)
    debtor_provider: str = Field(..., min_length=1)
    start_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    frequency: BillFrequency
    category_id: int
    end_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    is_active: bool = True
    next_due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class RecurringBillUpdate(BaseModel):
    name: str | None = None
    amount: Decimal | None = Field(None, gt=0)
    debtor_provider: str | None = None
    frequency: BillFrequency | None = None
    end_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    is_active: bool | None = None
    next_due_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class RecurringBillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    amount: Decimal
    debtor_provider: str
    start_date: str
    frequency: BillFrequency
    category_id: int
    end_date: str | None = None
    is_active: bool
    next_due_date: str
    created_at: datetime
    updated_at: datetime


# --- Salary Allocation Schemas ---

class SalaryAllocationToSinkingFundCreate(BaseModel):
    sinking_fund_id: int
    allocation_amount: Decimal = Field(..., ge=0)


class SalaryAllocationToSinkingFundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sinking_fund_id: int
    allocation_amount: Decimal


class SalaryAllocationCreate(BaseModel):
    monthly_salary_amount: Decimal = Field(..., gt=0)
    monthly_budget_allocation: Decimal = Field(..., ge=0)
    bills_fund_allocation_type: BillsAllocationMethod = BillsAllocationMethod.recommended
    bills_fund_fixed_amount: Decimal | None = Field(None, ge=0)
    sinking_fund_allocations: list[SalaryAllocationToSinkingFundCreate] = []


class SalaryAllocationUpdate(BaseModel):
    monthly_salary_amount: Decimal | None = Field(None, gt=0)
    monthly_budget_allocation: Decimal | None = Field(None, ge=0)
    bills_fund_allocation_type: BillsAllocationMethod | None = None
    bills_fund_fixed_amount: Decimal | None = Field(None, ge=0)
    sinking_fund_allocations: list[SalaryAllocationToSinkingFundCreate] | None = None


class SalaryAllocationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    monthly_salary_amount: Decimal
    monthly_budget_allocation: Decimal
    bills_fund_allocation_type: BillsAllocationMethod
    bills_fund_fixed_amount: Decimal | None = None
    sinking_fund_allocations: list[SalaryAllocationToSinkingFundResponse] = []
    created_at: datetime
    updated_at: datetime


# --- Monthly Unallocated Income Schemas ---

class MonthlyUnallocatedIncomeCreate(BaseModel):
    month: int = Field(..., ge=1, le=12)
    year: int = Field(..., ge=2000)
    unallocated_amount: Decimal = Field(default=Decimal("0"))


class MonthlyUnallocatedIncomeUpdate(BaseModel):
    unallocated_amount: Decimal | None = None


class MonthlyUnallocatedIncomeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    month: int
    year: int
    unallocated_amount: Decimal
    created_at: datetime
    updated_at: datetime


# --- Composite / API-specific Schemas ---

class AllocateRemainderRequest(BaseModel):
    """Request to manually allocate unallocated income to a sinking fund."""
    sinking_fund_id: int
    amount: Decimal = Field(..., gt=0)


class BudgetTransferRequest(BaseModel):
    """Transfer from a sinking fund to cover budget overspend."""
    sinking_fund_id: int
    budget_id: int
    amount: Decimal = Field(..., gt=0)


class DashboardSummary(BaseModel):
    """Aggregated dashboard data."""
    total_income: Decimal
    total_expenses: Decimal
    net: Decimal
    unallocated_income: Decimal
    budget_total_allocated: Decimal
    budget_total_spent: Decimal
    budget_total_remaining: Decimal
    sinking_funds: list[SinkingFundResponse]
    recent_transactions: list[TransactionResponse]
