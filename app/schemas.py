import re
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

# --- Enums ---


class CategoryType(str, Enum):
    income = "income"
    expense = "expense"
    transfer = "transfer"


class TransactionType(str, Enum):
    regular = "regular"
    income = "income"
    income_allocation = "income_allocation"
    secondary_income_allocation = "secondary_income_allocation"
    contribution = "contribution"
    withdrawal = "withdrawal"
    budget_expense = "budget_expense"
    budget_transfer = "budget_transfer"


class BillFrequency(str, Enum):
    weekly = "weekly"
    fortnightly = "fortnightly"
    twenty_eight_days = "28_days"
    monthly = "monthly"
    quarterly = "quarterly"
    yearly = "yearly"


class PaidFrom(str, Enum):
    bills_fund = "bills_fund"
    budget = "budget"
    sinking_fund = "sinking_fund"


# One 365-day basis for every annual figure. 28_days stays 13.036 (365 / 28).
FREQUENCY_LABELS: dict[str, str] = {
    "weekly": "Weekly",
    "fortnightly": "Fortnightly",
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "yearly": "Yearly",
    "28_days": "Every 28 Days",
}

FREQUENCY_ANNUAL_MULTIPLIER: dict[str, Decimal | int] = {
    "weekly": Decimal("52.143"),
    "fortnightly": Decimal("26.071"),
    "monthly": 12,
    "quarterly": 4,
    "yearly": 1,
    "28_days": Decimal("13.036"),
}


class BillsAllocationMethod(str, Enum):
    recommended = "recommended"
    fixed = "fixed"


class BillType(str, Enum):
    fixed = "fixed"
    variable = "variable"


class PaymentMethodKind(str, Enum):
    card = "card"
    bank = "bank"
    manual = "manual"


class ContractService(str, Enum):
    electricity = "electricity"
    health_insurance = "health_insurance"
    internet = "internet"
    phone = "phone"
    car_insurance = "car_insurance"
    other = "other"


class BillDelivery(str, Enum):
    email = "email"
    post = "post"
    app = "app"
    other = "other"


class ReplacementStatus(str, Enum):
    open = "open"
    finished = "finished"
    stopped = "stopped"


_BETWEEN_DIGITS = re.compile(r"(?<=\d)[ \-.]+(?=\d)")
_LONG_DIGIT_RUN = re.compile(r"\d{6,}")


def reject_account_number(value: str) -> str:
    """Reject a string that hides a card or account number behind separators."""
    collapsed = _BETWEEN_DIGITS.sub("", value)
    if _LONG_DIGIT_RUN.search(collapsed):
        raise PydanticCustomError(
            "account_number",
            "Don't put card or account numbers here.",
        )
    return value


def public_validation_errors(exc: ValidationError) -> list[dict[str, object]]:
    """Bills-API error shape: loc, msg, and type only. Never the input value."""
    return [
        {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
        for err in exc.errors()
    ]


def _optional_text(value: object) -> str | None:
    """Strip an optional string. Blank becomes null. Reject a hidden account number."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise PydanticCustomError("string_type", "Enter text.")
    text = value.strip()
    if text == "":
        return None
    return reject_account_number(text)


def _optional_iso_date(value: object) -> str | None:
    """Parse a real calendar date. Blank becomes null. Stored as YYYY-MM-DD."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise PydanticCustomError("date_type", "Enter a real date as YYYY-MM-DD.")
    text = value.strip()
    if text == "":
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise PydanticCustomError(
            "date_fromisoformat",
            "Enter a real date as YYYY-MM-DD.",
        ) from None
    return parsed.isoformat()


def _required_text(value: object) -> str:
    if not isinstance(value, str):
        raise PydanticCustomError("string_type", "Field required")
    text = value.strip()
    if text == "":
        raise PydanticCustomError("missing", "Field required")
    return reject_account_number(text)


def _optional_last4(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PydanticCustomError("last4", "Enter only the last 4 digits.")
    text = value.strip()
    if text == "":
        return None
    if re.fullmatch(r"\d{4}", text) is None:
        raise PydanticCustomError("last4", "Enter only the last 4 digits.")
    return text


class ShareTradeType(str, Enum):
    buy = "buy"
    sell = "sell"


class FxConvention(str, Enum):
    aud_usd = "AUD/USD"
    usd_aud = "USD/AUD"


class ShareCurrency(str, Enum):
    USD = "USD"
    AUD = "AUD"


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
    exclude_from_monthly_cost: bool = False


class CategoryUpdate(BaseModel):
    name: str | None = None
    type: CategoryType | None = None
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    is_budget_category: bool | None = None
    exclude_from_monthly_cost: bool | None = None


class CategoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    type: CategoryType
    color: str
    is_budget_category: bool
    is_deleted: bool
    is_system: bool
    exclude_from_monthly_cost: bool


# --- Transaction Schemas ---


class TransactionCreate(BaseModel):
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: str | None = None
    amount: Decimal = Field(..., gt=Decimal(0))
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
    amount: Decimal | None = Field(None, gt=Decimal(0))
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
    allocated_amount: Decimal = Field(..., ge=Decimal(0))
    spent_amount: Decimal = Field(default=Decimal(0), ge=Decimal(0))
    fund_balance: Decimal = Field(default=Decimal(0), ge=Decimal(0))


class BudgetUpdate(BaseModel):
    allocated_amount: Decimal | None = Field(None, ge=Decimal(0))
    spent_amount: Decimal | None = Field(None, ge=Decimal(0))
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


class BudgetRecommendation(BaseModel):
    """Advisory suggestion comparing allocated_amount to recent average spend."""

    model_config = ConfigDict(frozen=True)

    budget_id: int
    category_id: int
    category_name: str
    category_color: str
    current_allocated: Decimal
    recommended: Decimal
    delta: Decimal
    direction: str  # "raise" | "lower"
    months_used: int
    sample_spends: list[Decimal]


class BudgetRecommendationSummary(BaseModel):
    """Aggregate of budget recommendations for dashboard / panel footers."""

    model_config = ConfigDict(frozen=True)

    count: int
    net_delta: Decimal
    items: list[BudgetRecommendation]


# --- Sinking Fund Schemas ---


class SinkingFundCreate(BaseModel):
    name: str = Field(..., min_length=1)
    description: str | None = None
    current_balance: Decimal = Field(default=Decimal(0))
    color: str = Field(..., pattern=r"^#[0-9a-fA-F]{6}$")


class SinkingFundUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")


class SinkingFundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    current_balance: Decimal
    color: str
    is_deleted: bool
    is_system: bool
    created_at: datetime
    updated_at: datetime


# --- Recurring Bill Schemas ---


class RecurringBillCreate(BaseModel):
    name: str = Field(..., min_length=1)
    amount: Decimal = Field(..., gt=Decimal(0))
    debtor_provider: str = Field(..., min_length=1)
    start_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    frequency: BillFrequency
    category_id: int
    end_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    is_active: bool = True
    next_due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    bill_type: BillType = BillType.fixed
    foreign_amount: Decimal | None = Field(None, gt=Decimal(0))
    foreign_currency: str | None = Field(None, min_length=3, max_length=3)
    payment_method_id: int | None = None
    contract_id: int | None = None
    paid_from: PaidFrom = PaidFrom.bills_fund
    paid_from_fund_id: int | None = None


class RecurringBillUpdate(BaseModel):
    name: str | None = None
    amount: Decimal | None = Field(None, gt=Decimal(0))
    debtor_provider: str | None = None
    frequency: BillFrequency | None = None
    category_id: int | None = None
    end_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    is_active: bool | None = None
    next_due_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    bill_type: BillType | None = None
    foreign_amount: Decimal | None = Field(None, gt=Decimal(0))
    foreign_currency: str | None = Field(None, min_length=3, max_length=3)
    payment_method_id: int | None = None
    contract_id: int | None = None
    paid_from: PaidFrom | None = None
    paid_from_fund_id: int | None = None


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
    bill_type: str
    next_due_date: str
    foreign_amount: Decimal | None = None
    foreign_currency: str | None = None
    payment_method_id: int | None = None
    contract_id: int | None = None
    paid_from: str = "bills_fund"
    paid_from_fund_id: int | None = None
    created_at: datetime
    updated_at: datetime


class RecurringBillPay(BaseModel):
    amount: Decimal = Field(..., gt=Decimal(0))
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class RecurringBillConfirm(BaseModel):
    amount: Decimal = Field(..., gt=Decimal(0))
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("amount")
    @classmethod
    def _at_most_two_places(cls, value: Decimal) -> Decimal:
        exponent = value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise ValueError("Amount must have at most 2 decimal places.")
        return value


class RecurringBillSkip(BaseModel):
    due_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class DuePaymentResponse(BaseModel):
    bill_id: int
    name: str
    provider: str
    expected_amount: Decimal
    due_date: str
    more_due: int
    paid_from: str
    paid_from_label: str
    budget_month: str | None = None
    problem: dict[str, str] | None = None


class PaymentMethodCreate(BaseModel):
    nickname: str = Field(..., max_length=60)
    kind: PaymentMethodKind
    last4: str | None = None
    expiry_month: int | None = Field(None, ge=1, le=12)
    expiry_year: int | None = Field(None, ge=2000, le=2100)
    owner: str = Field(..., max_length=60)

    @field_validator("nickname", "owner", mode="before")
    @classmethod
    def _clean_required(cls, value: object) -> str:
        return _required_text(value)

    @field_validator("last4", mode="before")
    @classmethod
    def _clean_last4(cls, value: object) -> str | None:
        return _optional_last4(value)


class PaymentMethodUpdate(BaseModel):
    nickname: str | None = Field(None, max_length=60)
    kind: PaymentMethodKind | None = None
    last4: str | None = None
    expiry_month: int | None = Field(None, ge=1, le=12)
    expiry_year: int | None = Field(None, ge=2000, le=2100)
    owner: str | None = Field(None, max_length=60)

    @field_validator("nickname", "owner", mode="before")
    @classmethod
    def _clean_optional(cls, value: object) -> str | None:
        if value is None:
            return None
        return _required_text(value)

    @field_validator("last4", mode="before")
    @classmethod
    def _clean_last4(cls, value: object) -> str | None:
        return _optional_last4(value)


class PaymentMethodResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    nickname: str
    kind: PaymentMethodKind
    last4: str | None = None
    expiry_month: int | None = None
    expiry_year: int | None = None
    owner: str
    is_archived: bool
    created_at: datetime
    updated_at: datetime


class ContractCreate(BaseModel):
    name: str = Field(..., max_length=100)
    provider: str = Field(..., max_length=150)
    plan_name: str | None = Field(None, max_length=150)
    service: ContractService
    bill_delivery: BillDelivery
    delivery_detail: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=500)
    started_on: str | None = None
    ended_on: str | None = None

    @field_validator("name", "provider", mode="before")
    @classmethod
    def _clean_required(cls, value: object) -> str:
        return _required_text(value)

    @field_validator("plan_name", "delivery_detail", "notes", mode="before")
    @classmethod
    def _clean_optional(cls, value: object) -> str | None:
        return _optional_text(value)

    @field_validator("started_on", "ended_on", mode="before")
    @classmethod
    def _clean_dates(cls, value: object) -> str | None:
        return _optional_iso_date(value)

    @model_validator(mode="after")
    def _end_not_before_start(self):
        if (
            self.started_on is not None
            and self.ended_on is not None
            and self.ended_on < self.started_on
        ):
            raise ValueError("End date must be on or after the start date.")
        return self


class ContractUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    provider: str | None = Field(None, max_length=150)
    plan_name: str | None = Field(None, max_length=150)
    service: ContractService | None = None
    bill_delivery: BillDelivery | None = None
    delivery_detail: str | None = Field(None, max_length=200)
    notes: str | None = Field(None, max_length=500)
    started_on: str | None = None
    ended_on: str | None = None

    @field_validator("name", "provider", mode="before")
    @classmethod
    def _clean_required(cls, value: object) -> str | None:
        if value is None:
            return None
        return _required_text(value)

    @field_validator("plan_name", "delivery_detail", "notes", mode="before")
    @classmethod
    def _clean_optional(cls, value: object) -> str | None:
        return _optional_text(value)

    @field_validator("started_on", "ended_on", mode="before")
    @classmethod
    def _clean_dates(cls, value: object) -> str | None:
        return _optional_iso_date(value)


class ContractResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    provider: str
    plan_name: str | None = None
    service: ContractService
    bill_delivery: BillDelivery
    delivery_detail: str | None = None
    notes: str | None = None
    started_on: str | None = None
    ended_on: str | None = None
    created_at: datetime
    updated_at: datetime


class ReplacementItemResponse(BaseModel):
    bill_id: int
    name: str
    done: bool
    due_soon: bool
    updated_on: str | None = None
    note: str | None = None


class ReplacementProgressResponse(BaseModel):
    old_id: int
    new_id: int
    other_id: int
    other_label: str
    started_at: datetime
    done: int
    counted: int
    items: list[ReplacementItemResponse]


class ReplacementNoteInput(BaseModel):
    note: str | None = Field(None, max_length=200)
    updated_on: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("note", mode="before")
    @classmethod
    def _note(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise PydanticCustomError("string_type", "Enter a note.")
        text = value.strip()
        if text == "":
            return None
        return reject_account_number(text)


# --- Income Allocation Schemas ---


class IncomeAllocationToSinkingFundCreate(BaseModel):
    sinking_fund_id: int
    allocation_amount: Decimal = Field(..., ge=Decimal(0))


class IncomeAllocationToSinkingFundResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sinking_fund_id: int
    allocation_amount: Decimal


class IncomeAllocationRecurringTransferCreate(BaseModel):
    description: str = Field(..., min_length=1, max_length=200)
    amount: Decimal = Field(..., gt=Decimal(0))


class IncomeAllocationRecurringTransferResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str
    amount: Decimal


class IncomeAllocationCreate(BaseModel):
    monthly_income_amount: Decimal = Field(..., gt=Decimal(0))
    monthly_budget_allocation: Decimal = Field(..., ge=Decimal(0))
    bills_fund_allocation_type: BillsAllocationMethod = (
        BillsAllocationMethod.recommended
    )
    bills_fund_fixed_amount: Decimal | None = Field(None, ge=Decimal(0))
    sinking_fund_allocations: list[IncomeAllocationToSinkingFundCreate] = []
    recurring_transfers: list[IncomeAllocationRecurringTransferCreate] = []
    overflow_sinking_fund_id: int | None = None


class IncomeAllocationUpdate(BaseModel):
    monthly_income_amount: Decimal | None = Field(None, gt=Decimal(0))
    monthly_budget_allocation: Decimal | None = Field(None, ge=Decimal(0))
    bills_fund_allocation_type: BillsAllocationMethod | None = None
    bills_fund_fixed_amount: Decimal | None = Field(None, ge=Decimal(0))
    sinking_fund_allocations: list[IncomeAllocationToSinkingFundCreate] | None = None
    overflow_sinking_fund_id: int | None = None


class IncomeAllocationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    monthly_income_amount: Decimal
    monthly_budget_allocation: Decimal
    bills_fund_allocation_type: BillsAllocationMethod
    bills_fund_fixed_amount: Decimal | None = None
    sinking_fund_allocations: list[IncomeAllocationToSinkingFundResponse] = []
    recurring_transfers: list[IncomeAllocationRecurringTransferResponse] = []
    overflow_sinking_fund_id: int | None = None
    created_at: datetime
    updated_at: datetime


# --- Secondary Income Allocation Schemas ---


class SecondaryIncomeAllocationRuleCreate(BaseModel):
    sinking_fund_id: int
    goal_amount: Decimal = Field(..., gt=Decimal(0))
    sort_order: int = Field(default=0, ge=0)


class SecondaryIncomeAllocationRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    sinking_fund_id: int
    goal_amount: Decimal
    sort_order: int


class SecondaryIncomeAllocationCreate(BaseModel):
    label: str = Field(default="Secondary Income", min_length=1, max_length=100)
    rules: list[SecondaryIncomeAllocationRuleCreate] = []
    overflow_sinking_fund_id: int | None = None


class SecondaryIncomeAllocationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    rules: list[SecondaryIncomeAllocationRuleResponse] = []
    overflow_sinking_fund_id: int | None = None
    created_at: datetime
    updated_at: datetime


class RecordSecondaryIncomeRequest(BaseModel):
    amount: Decimal = Field(..., gt=Decimal(0))
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: str | None = Field(None, max_length=200)


# --- Monthly Unallocated Income Schemas ---


class MonthlyUnallocatedIncomeCreate(BaseModel):
    month: int = Field(..., ge=1, le=12)
    year: int = Field(..., ge=2000)
    unallocated_amount: Decimal = Field(default=Decimal(0))


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


# --- API Key Schemas ---


class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=100)


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class ApiKeyCreatedResponse(BaseModel):
    """Returned only on creation — the only time the plaintext key is visible."""

    key: str
    api_key: ApiKeyResponse


# --- Composite / API-specific Schemas ---


class AllocateRemainderRequest(BaseModel):
    """Request to manually allocate unallocated income to a sinking fund."""

    sinking_fund_id: int
    amount: Decimal = Field(..., gt=Decimal(0))


class BudgetTransferRequest(BaseModel):
    """Transfer from a sinking fund to cover budget overspend."""

    sinking_fund_id: int
    budget_id: int
    amount: Decimal = Field(..., gt=Decimal(0))


class ShareTradeCreate(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    trade_type: ShareTradeType
    trade_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    quantity: Decimal = Field(..., gt=Decimal(0))
    price: Decimal = Field(..., gt=Decimal(0))
    fee: Decimal = Field(default=Decimal(0), ge=Decimal(0))
    fee_currency: ShareCurrency = ShareCurrency.USD
    fx_rate: Decimal | None = Field(default=None, gt=Decimal(0))
    fx_input_convention: FxConvention | None = None
    notes: str | None = Field(default=None, max_length=500)


class ShareTradeUpdate(ShareTradeCreate):
    """A full replacement of a trade. FIFO is rechecked by the route."""


class SharesHoldingResponse(BaseModel):
    ticker: str
    quantity: float
    currency: str
    cost_aud: float
    value_usd: float | None = None
    value_aud: float | None = None
    unrealised_aud: float | None = None
    unrealised_usd: float | None = None
    pct: float | None = None
    price: float | None = None
    price_date: str | None = None


class SharesSummaryResponse(BaseModel):
    financial_year: str
    cost_aud: float
    value_aud: float
    value_usd: float
    unrealised_aud: float
    unrealised_usd: float
    realised_aud: float
    unpriced_count: int
    fx_rate: float | None = None
    fx_date: str | None = None
    fx_source: str | None = None
    price_as_of: str | None = None
    holdings: list[SharesHoldingResponse]
    realised_by_fy: dict[str, float]


class DashboardSummary(BaseModel):
    """Aggregated dashboard data."""

    total_income: Decimal
    total_expenses: Decimal
    net: Decimal
    unallocated_income: Decimal
    budget_total_allocated: Decimal
    budget_total_spent: Decimal
    budget_total_remaining: Decimal
    total_sinking_funds: Decimal
    total_net_worth: Decimal
    sinking_funds: list[SinkingFundResponse]
    recent_transactions: list[TransactionResponse]
