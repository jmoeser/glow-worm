"""Non-interactive script to populate the database with realistic sample data."""

import calendar
import sys
from datetime import date, timedelta

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import (
    Budget,
    Category,
    MonthlyUnallocatedIncome,
    RecurringBill,
    IncomeAllocation,
    IncomeAllocationToSinkingFund,
    SinkingFund,
    Transaction,
    User,
)
from app.schemas import (
    BillFrequency,
    BillsAllocationMethod,
    CategoryType,
    TransactionType,
)


def seed_data() -> None:
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Guard: require at least one user
        user = db.execute(select(User)).scalar_one_or_none()
        if not user:
            print("Error: No user found. Run 'uv run python scripts/create_user.py' first.")
            sys.exit(1)

        # Guard: prevent duplicate seeding
        existing = db.execute(select(Category)).scalars().first()
        if existing:
            print("Seed data already exists. Aborting to prevent duplicates.")
            sys.exit(0)

        today = date.today()
        current_month = today.month
        current_year = today.year
        first_of_month = date(current_year, current_month, 1)

        # --- Categories ---
        income_cat = Category(
            name="Salary", type=CategoryType.income.value, color="#22C55E",
            is_budget_category=False,
        )
        groceries_cat = Category(
            name="Groceries", type=CategoryType.expense.value, color="#EF4444",
            is_budget_category=True,
        )
        dining_cat = Category(
            name="Dining Out", type=CategoryType.expense.value, color="#F97316",
            is_budget_category=True,
        )
        transport_cat = Category(
            name="Transport", type=CategoryType.expense.value, color="#3B82F6",
            is_budget_category=True,
        )
        entertainment_cat = Category(
            name="Entertainment", type=CategoryType.expense.value, color="#8B5CF6",
            is_budget_category=True,
        )
        health_cat = Category(
            name="Health", type=CategoryType.expense.value, color="#EC4899",
            is_budget_category=True,
        )
        clothing_cat = Category(
            name="Clothing", type=CategoryType.expense.value, color="#14B8A6",
            is_budget_category=True,
        )
        household_cat = Category(
            name="Household", type=CategoryType.expense.value, color="#F59E0B",
            is_budget_category=True,
        )

        categories = [
            income_cat, groceries_cat, dining_cat, transport_cat,
            entertainment_cat, health_cat, clothing_cat, household_cat,
        ]
        db.add_all(categories)
        db.flush()

        # --- Sinking Funds ---
        bills_fund = SinkingFund(
            name="Bills", color="#EF4444",
            monthly_allocation=800, current_balance=800,
        )
        short_term_fund = SinkingFund(
            name="Short Term Savings", color="#3B82F6",
            monthly_allocation=300, current_balance=1200,
        )
        long_term_fund = SinkingFund(
            name="Long Term Savings", color="#22C55E",
            monthly_allocation=500, current_balance=6000,
        )
        emergency_fund = SinkingFund(
            name="Emergency Fund", color="#F59E0B",
            monthly_allocation=200, current_balance=3000,
        )

        funds = [bills_fund, short_term_fund, long_term_fund, emergency_fund]
        db.add_all(funds)
        db.flush()

        # --- Recurring Bills (linked to a "Bills" expense category) ---
        # We need an expense category for bills — use household_cat as a reasonable fit,
        # or create a dedicated one. The plan says "linked to a Bills expense category".
        # Since there's no Bills category in the categories list, we'll create one.
        bills_cat = Category(
            name="Bills", type=CategoryType.expense.value, color="#EF4444",
            is_budget_category=False,
        )
        db.add(bills_cat)
        db.flush()

        def add_months(d: date, months: int) -> date:
            """Add months to a date, clamping to last day of target month."""
            m = d.month - 1 + months
            year = d.year + m // 12
            month = m % 12 + 1
            day = min(d.day, calendar.monthrange(year, month)[1])
            return date(year, month, day)

        next_month = add_months(first_of_month, 1)

        rent = RecurringBill(
            name="Rent", amount=2400, debtor_provider="Landlord",
            start_date=first_of_month.isoformat(), frequency=BillFrequency.monthly.value,
            category_id=bills_cat.id, next_due_date=next_month.isoformat(),
        )
        electricity = RecurringBill(
            name="Electricity", amount=150, debtor_provider="Energy Co",
            start_date=first_of_month.isoformat(), frequency=BillFrequency.quarterly.value,
            category_id=bills_cat.id,
            next_due_date=add_months(first_of_month, 3).isoformat(),
        )
        internet = RecurringBill(
            name="Internet", amount=89, debtor_provider="ISP Co",
            start_date=first_of_month.isoformat(), frequency=BillFrequency.monthly.value,
            category_id=bills_cat.id, next_due_date=next_month.isoformat(),
        )
        phone = RecurringBill(
            name="Phone", amount=55, debtor_provider="Telco",
            start_date=first_of_month.isoformat(), frequency=BillFrequency.monthly.value,
            category_id=bills_cat.id, next_due_date=next_month.isoformat(),
        )
        car_insurance = RecurringBill(
            name="Car Insurance", amount=1200, debtor_provider="Insurer Co",
            start_date=first_of_month.isoformat(), frequency=BillFrequency.yearly.value,
            category_id=bills_cat.id,
            next_due_date=date(current_year + 1, current_month, 1).isoformat(),
        )

        bills = [rent, electricity, internet, phone, car_insurance]
        db.add_all(bills)
        db.flush()

        # --- Budgets (current month, one per expense budget category) ---
        budget_data = [
            (groceries_cat, 600, 245.80),
            (dining_cat, 200, 87.50),
            (transport_cat, 150, 62.00),
            (entertainment_cat, 100, 35.00),
            (health_cat, 150, 0),
            (clothing_cat, 100, 0),
            (household_cat, 200, 48.90),
        ]

        budgets = []
        for cat, allocated, spent in budget_data:
            b = Budget(
                category_id=cat.id, month=current_month, year=current_year,
                allocated_amount=allocated, spent_amount=spent,
            )
            budgets.append(b)
        db.add_all(budgets)
        db.flush()

        # Map categories to their budgets for transaction linking
        cat_to_budget = {b.category_id: b for b in budgets}

        # --- Income Allocation ---
        income_alloc = IncomeAllocation(
            monthly_income_amount=6500,
            monthly_budget_allocation=1500,
            bills_fund_allocation_type=BillsAllocationMethod.recommended.value,
        )
        db.add(income_alloc)
        db.flush()

        junction_data = [
            (bills_fund, 800),
            (short_term_fund, 300),
            (long_term_fund, 500),
            (emergency_fund, 200),
        ]
        for fund, amount in junction_data:
            db.add(IncomeAllocationToSinkingFund(
                income_allocation_id=income_alloc.id,
                sinking_fund_id=fund.id,
                allocation_amount=amount,
            ))
        db.flush()

        # --- Transactions ---

        # 1. Income on the 1st
        db.add(Transaction(
            date=first_of_month.isoformat(), description="Monthly income",
            amount=6500, category_id=income_cat.id,
            type=CategoryType.income.value,
            transaction_type=TransactionType.income.value,
        ))

        # 2. Income allocation transactions on the 1st
        db.add(Transaction(
            date=first_of_month.isoformat(), description="Budget allocation",
            amount=1500, category_id=income_cat.id,
            type=CategoryType.expense.value,
            transaction_type=TransactionType.income_allocation.value,
        ))
        for fund, amount in junction_data:
            db.add(Transaction(
                date=first_of_month.isoformat(),
                description=f"Income allocation to {fund.name}",
                amount=amount, category_id=income_cat.id,
                type=CategoryType.expense.value,
                transaction_type=TransactionType.income_allocation.value,
                sinking_fund_id=fund.id,
            ))

        # 3. Budget expense transactions spread through the month
        expense_transactions = [
            (groceries_cat, "Woolworths groceries", 89.50, 3),
            (groceries_cat, "Aldi weekly shop", 67.30, 7),
            (groceries_cat, "Coles midweek top-up", 89.00, 14),
            (dining_cat, "Pizza night", 42.50, 5),
            (dining_cat, "Cafe brunch", 45.00, 10),
            (transport_cat, "Fuel", 62.00, 4),
            (entertainment_cat, "Cinema tickets", 35.00, 8),
            (household_cat, "Cleaning supplies", 48.90, 6),
        ]

        for cat, desc, amount, day_offset in expense_transactions:
            tx_date = first_of_month + timedelta(days=day_offset)
            # Don't create transactions in the future
            if tx_date > today:
                continue
            db.add(Transaction(
                date=tx_date.isoformat(), description=desc,
                amount=amount, category_id=cat.id,
                type=CategoryType.expense.value,
                transaction_type=TransactionType.budget_expense.value,
                budget_id=cat_to_budget[cat.id].id,
            ))

        # 4. One contribution to a sinking fund
        contrib_date = first_of_month + timedelta(days=12)
        if contrib_date <= today:
            db.add(Transaction(
                date=contrib_date.isoformat(),
                description="Extra savings contribution",
                amount=100, category_id=income_cat.id,
                type=CategoryType.expense.value,
                transaction_type=TransactionType.contribution.value,
                sinking_fund_id=short_term_fund.id,
            ))

        # --- Monthly Unallocated Income ---
        # Salary 6500 - Budget 1500 - Bills 800 - Short Term 300
        # - Long Term 500 - Emergency 200 = 3200
        db.add(MonthlyUnallocatedIncome(
            month=current_month, year=current_year,
            unallocated_amount=3200,
        ))

        db.commit()
        print("Seed data created successfully.")
        print(f"  - 9 categories")
        print(f"  - 4 sinking funds")
        print(f"  - 5 recurring bills")
        print(f"  - 7 budgets ({current_month}/{current_year})")
        print(f"  - 1 income allocation with 4 fund links")
        print(f"  - Transactions for the current month")
        print(f"  - 1 monthly unallocated income record")

    finally:
        db.close()


if __name__ == "__main__":
    seed_data()
