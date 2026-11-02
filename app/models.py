from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.schemas import (
    BillsAllocationMethod,
    TransactionType,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    session_version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    api_keys: Mapped[list[ApiKey]] = relationship(back_populates="user")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    is_budget_category: Mapped[bool] = mapped_column(default=False)
    is_deleted: Mapped[bool] = mapped_column(default=False)
    is_system: Mapped[bool] = mapped_column(default=False)
    exclude_from_monthly_cost: Mapped[bool] = mapped_column(default=False)

    transactions: Mapped[list[Transaction]] = relationship(back_populates="category")
    budgets: Mapped[list[Budget]] = relationship(back_populates="category")
    recurring_bills: Mapped[list[RecurringBill]] = relationship(
        back_populates="category"
    )


class SinkingFund(Base):
    __tablename__ = "sinking_funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    current_balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(default=False)
    is_system: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="sinking_fund"
    )
    income_allocations: Mapped[list[IncomeAllocationToSinkingFund]] = relationship(
        back_populates="sinking_fund"
    )


class RecurringBill(Base):
    __tablename__ = "recurring_bills"
    __table_args__ = (
        Index("ix_recurring_bills_payment_method_id", "payment_method_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    debtor_provider: Mapped[str] = mapped_column(String(150), nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id"), nullable=False
    )
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    bill_type: Mapped[str] = mapped_column(String(10), nullable=False, default="fixed")
    next_due_date: Mapped[str] = mapped_column(String(10), nullable=False)
    foreign_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    foreign_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    payment_method_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "payment_methods.id",
            name="fk_recurring_bills_payment_method_id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    category: Mapped[Category] = relationship(back_populates="recurring_bills")
    payment_method: Mapped[PaymentMethod | None] = relationship(back_populates="bills")
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="recurring_bill"
    )


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("category_id", "month", "year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id"), nullable=False
    )
    month: Mapped[int] = mapped_column(nullable=False)
    year: Mapped[int] = mapped_column(nullable=False)
    allocated_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    spent_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    fund_balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    category: Mapped[Category] = relationship(back_populates="budgets")
    transactions: Mapped[list[Transaction]] = relationship(back_populates="budget")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(10), nullable=False)
    transaction_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default=TransactionType.regular.value
    )
    sinking_fund_id: Mapped[int | None] = mapped_column(
        ForeignKey("sinking_funds.id"), nullable=True
    )
    recurring_bill_id: Mapped[int | None] = mapped_column(
        ForeignKey("recurring_bills.id"), nullable=True
    )
    budget_id: Mapped[int | None] = mapped_column(
        ForeignKey("budgets.id"), nullable=True
    )
    is_paid: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    category: Mapped[Category] = relationship(back_populates="transactions")
    sinking_fund: Mapped[SinkingFund | None] = relationship(
        back_populates="transactions"
    )
    recurring_bill: Mapped[RecurringBill | None] = relationship(
        back_populates="transactions"
    )
    budget: Mapped[Budget | None] = relationship(back_populates="transactions")


class IncomeAllocation(Base):
    __tablename__ = "income_allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    monthly_income_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    monthly_budget_allocation: Mapped[float] = mapped_column(
        Numeric(12, 2), nullable=False
    )
    bills_fund_allocation_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BillsAllocationMethod.recommended.value
    )
    bills_fund_fixed_amount: Mapped[float | None] = mapped_column(
        Numeric(12, 2), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    overflow_sinking_fund_id: Mapped[int | None] = mapped_column(
        ForeignKey("sinking_funds.id"), nullable=True
    )

    sinking_fund_allocations: Mapped[list[IncomeAllocationToSinkingFund]] = (
        relationship(back_populates="income_allocation", cascade="all, delete-orphan")
    )
    recurring_transfers: Mapped[list[IncomeAllocationRecurringTransfer]] = relationship(
        back_populates="income_allocation", cascade="all, delete-orphan"
    )
    overflow_sinking_fund: Mapped[SinkingFund | None] = relationship(
        "SinkingFund",
        foreign_keys="[IncomeAllocation.overflow_sinking_fund_id]",
    )


class IncomeAllocationToSinkingFund(Base):
    __tablename__ = "income_allocation_to_sinking_funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    income_allocation_id: Mapped[int] = mapped_column(
        ForeignKey("income_allocations.id"), nullable=False
    )
    sinking_fund_id: Mapped[int] = mapped_column(
        ForeignKey("sinking_funds.id"), nullable=False
    )
    allocation_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)

    income_allocation: Mapped[IncomeAllocation] = relationship(
        back_populates="sinking_fund_allocations"
    )
    sinking_fund: Mapped[SinkingFund] = relationship(
        back_populates="income_allocations"
    )


class IncomeAllocationRecurringTransfer(Base):
    __tablename__ = "income_allocation_recurring_transfers"

    id: Mapped[int] = mapped_column(primary_key=True)
    income_allocation_id: Mapped[int] = mapped_column(
        ForeignKey("income_allocations.id"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)

    income_allocation: Mapped[IncomeAllocation] = relationship(
        back_populates="recurring_transfers"
    )


class SecondaryIncomeAllocation(Base):
    __tablename__ = "secondary_income_allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(
        String(100), nullable=False, default="Secondary Income"
    )
    overflow_sinking_fund_id: Mapped[int | None] = mapped_column(
        ForeignKey("sinking_funds.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    rules: Mapped[list[SecondaryIncomeAllocationRule]] = relationship(
        back_populates="secondary_income_allocation", cascade="all, delete-orphan"
    )
    overflow_sinking_fund: Mapped[SinkingFund | None] = relationship(
        "SinkingFund",
        foreign_keys="[SecondaryIncomeAllocation.overflow_sinking_fund_id]",
    )


class SecondaryIncomeAllocationRule(Base):
    __tablename__ = "secondary_income_allocation_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    secondary_income_allocation_id: Mapped[int] = mapped_column(
        ForeignKey("secondary_income_allocations.id"), nullable=False
    )
    sinking_fund_id: Mapped[int] = mapped_column(
        ForeignKey("sinking_funds.id"), nullable=False
    )
    goal_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    sort_order: Mapped[int] = mapped_column(nullable=False, default=0)

    secondary_income_allocation: Mapped[SecondaryIncomeAllocation] = relationship(
        back_populates="rules"
    )
    sinking_fund: Mapped[SinkingFund] = relationship()


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="default")
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")


class ShareTrade(Base):
    __tablename__ = "share_trades"
    __table_args__ = (Index("ix_share_trades_ticker_date", "ticker", "trade_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    trade_type: Mapped[str] = mapped_column(String(4), nullable=False)
    trade_date: Mapped[str] = mapped_column(String(10), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal(0)
    )
    fee_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    fx_rate_aud_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True
    )
    fx_rate_input: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True
    )
    fx_input_convention: Mapped[str | None] = mapped_column(String(7), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class SharePrice(Base):
    __tablename__ = "share_prices"
    __table_args__ = (
        UniqueConstraint("ticker", "price_date", name="uq_share_prices_ticker_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    price_date: Mapped[str] = mapped_column(String(10), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(default=_utcnow)


class FxRate(Base):
    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("base", "quote", "rate_date", name="uq_fx_rates_pair_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    base: Mapped[str] = mapped_column(String(3), nullable=False)
    quote: Mapped[str] = mapped_column(String(3), nullable=False)
    rate_date: Mapped[str] = mapped_column(String(10), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 10), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(default=_utcnow)


class MarketDataRun(Base):
    __tablename__ = "market_data_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="running")
    av_requests: Mapped[int] = mapped_column(default=0)
    tickers_updated: Mapped[int] = mapped_column(default=0)
    fx_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    message: Mapped[str | None] = mapped_column(String(1000), nullable=True)


class PaymentMethod(Base):
    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(primary_key=True)
    nickname: Mapped[str] = mapped_column(String(60), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    expiry_month: Mapped[int | None] = mapped_column(nullable=True)
    expiry_year: Mapped[int | None] = mapped_column(nullable=True)
    owner: Mapped[str] = mapped_column(String(60), nullable=False)
    is_archived: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    bills: Mapped[list[RecurringBill]] = relationship(back_populates="payment_method")
    replacements_as_old: Mapped[list[PaymentMethodReplacement]] = relationship(
        back_populates="old",
        foreign_keys="PaymentMethodReplacement.old_id",
    )
    replacements_as_new: Mapped[list[PaymentMethodReplacement]] = relationship(
        back_populates="new",
        foreign_keys="PaymentMethodReplacement.new_id",
    )


class PaymentMethodReplacement(Base):
    __tablename__ = "payment_method_replacements"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'finished', 'stopped')",
            name="ck_pm_replacements_status",
        ),
        CheckConstraint("old_id != new_id", name="ck_pm_replacements_distinct"),
        Index(
            "uq_pm_replacements_open_old",
            "old_id",
            unique=True,
            sqlite_where=text("status = 'open'"),
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    old_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id"), nullable=False, index=True
    )
    new_id: Mapped[int] = mapped_column(
        ForeignKey("payment_methods.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="open")
    started_at: Mapped[datetime] = mapped_column(default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)

    old: Mapped[PaymentMethod] = relationship(
        back_populates="replacements_as_old",
        foreign_keys=[old_id],
    )
    new: Mapped[PaymentMethod] = relationship(
        back_populates="replacements_as_new",
        foreign_keys=[new_id],
    )
    items: Mapped[list[PaymentMethodReplacementItem]] = relationship(
        back_populates="replacement",
        cascade="all, delete-orphan",
    )


class PaymentMethodReplacementItem(Base):
    __tablename__ = "payment_method_replacement_items"
    __table_args__ = (
        UniqueConstraint(
            "replacement_id",
            "recurring_bill_id",
            name="uq_pm_replacement_items_rep_bill",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    replacement_id: Mapped[int] = mapped_column(
        ForeignKey("payment_method_replacements.id"), nullable=False
    )
    recurring_bill_id: Mapped[int] = mapped_column(
        ForeignKey("recurring_bills.id"), nullable=False
    )
    updated_on: Mapped[str | None] = mapped_column(String(10), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    replacement: Mapped[PaymentMethodReplacement] = relationship(back_populates="items")
    recurring_bill: Mapped[RecurringBill] = relationship()


class MonthlyUnallocatedIncome(Base):
    __tablename__ = "monthly_unallocated_income"
    __table_args__ = (UniqueConstraint("month", "year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[int] = mapped_column(nullable=False)
    year: Mapped[int] = mapped_column(nullable=False)
    unallocated_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
