from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.schemas import (
    BillFrequency,
    BillsAllocationMethod,
    CategoryType,
    TransactionType,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    is_budget_category: Mapped[bool] = mapped_column(default=False)
    is_deleted: Mapped[bool] = mapped_column(default=False)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="category")
    budgets: Mapped[list["Budget"]] = relationship(back_populates="category")
    recurring_bills: Mapped[list["RecurringBill"]] = relationship(back_populates="category")


class SinkingFund(Base):
    __tablename__ = "sinking_funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    monthly_allocation: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    current_balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    transactions: Mapped[list["Transaction"]] = relationship(back_populates="sinking_fund")
    salary_allocations: Mapped[list["SalaryAllocationToSinkingFund"]] = relationship(
        back_populates="sinking_fund"
    )


class RecurringBill(Base):
    __tablename__ = "recurring_bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    debtor_provider: Mapped[str] = mapped_column(String(150), nullable=False)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    next_due_date: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    category: Mapped["Category"] = relationship(back_populates="recurring_bills")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="recurring_bill")


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("category_id", "month", "year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    month: Mapped[int] = mapped_column(nullable=False)
    year: Mapped[int] = mapped_column(nullable=False)
    allocated_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    spent_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    fund_balance: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    category: Mapped["Category"] = relationship(back_populates="budgets")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="budget")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[str] = mapped_column(String(10), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(10), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False, default=TransactionType.regular.value)
    sinking_fund_id: Mapped[int | None] = mapped_column(ForeignKey("sinking_funds.id"), nullable=True)
    recurring_bill_id: Mapped[int | None] = mapped_column(ForeignKey("recurring_bills.id"), nullable=True)
    budget_id: Mapped[int | None] = mapped_column(ForeignKey("budgets.id"), nullable=True)
    is_paid: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)

    category: Mapped["Category"] = relationship(back_populates="transactions")
    sinking_fund: Mapped["SinkingFund | None"] = relationship(back_populates="transactions")
    recurring_bill: Mapped["RecurringBill | None"] = relationship(back_populates="transactions")
    budget: Mapped["Budget | None"] = relationship(back_populates="transactions")


class SalaryAllocation(Base):
    __tablename__ = "salary_allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    monthly_salary_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    monthly_budget_allocation: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    bills_fund_allocation_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BillsAllocationMethod.recommended.value
    )
    bills_fund_fixed_amount: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)

    sinking_fund_allocations: Mapped[list["SalaryAllocationToSinkingFund"]] = relationship(
        back_populates="salary_allocation", cascade="all, delete-orphan"
    )


class SalaryAllocationToSinkingFund(Base):
    __tablename__ = "salary_allocation_to_sinking_funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    salary_allocation_id: Mapped[int] = mapped_column(
        ForeignKey("salary_allocations.id"), nullable=False
    )
    sinking_fund_id: Mapped[int] = mapped_column(
        ForeignKey("sinking_funds.id"), nullable=False
    )
    allocation_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)

    salary_allocation: Mapped["SalaryAllocation"] = relationship(
        back_populates="sinking_fund_allocations"
    )
    sinking_fund: Mapped["SinkingFund"] = relationship(back_populates="salary_allocations")


class MonthlyUnallocatedIncome(Base):
    __tablename__ = "monthly_unallocated_income"
    __table_args__ = (UniqueConstraint("month", "year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[int] = mapped_column(nullable=False)
    year: Mapped[int] = mapped_column(nullable=False)
    unallocated_amount: Mapped[float] = mapped_column(Numeric(12, 2), default=0)
    created_at: Mapped[datetime] = mapped_column(default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=_utcnow, onupdate=_utcnow)
