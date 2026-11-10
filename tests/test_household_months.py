"""Month records, bootstrap, recovery, and claims.

Concurrency uses two connections to a file database. The shared in-memory
test engine cannot show that, and it cannot show PostgreSQL locking.
"""

import os
import subprocess
import threading
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.config import TIMEZONE
from app.database import Base
from app.models import (
    Budget,
    Category,
    HouseholdMonth,
    HouseholdProcessing,
    IncomeAllocation,
    IncomeAllocationToSinkingFund,
    MonthlyUnallocatedIncome,
    RecurringBill,
    SinkingFund,
    Transaction,
)
from app.services.clock import month_window_open
from app.services.household_months import (
    automatic_recovery_allowed,
    ensure_backfill,
    processed_claim_statement,
    recover_current_month,
    settlement_claim_statement,
)
from app.services.sweep_descriptions import income_description, sweep_descriptions
from app.services.transaction_balances import apply_transaction_balance_effects
from app.tasks import process_income_allocation
from scripts.review_monthly_processing import apply_current_month, report

pytestmark = pytest.mark.usefixtures("setup_database")


def _ready(
    db, *, income="5000", overflow_balance="0", with_bills=True, with_overflow=False
):
    income_cat = Category(name="Salary", type="income", color="#00FF00")
    expense_cat = Category(name="Bills", type="expense", color="#FF0000")
    transfer_cat = Category(name="Transfer", type="transfer", color="#6B7280")
    budget_cat = Category(
        name="Groceries", type="expense", color="#22C55E", is_budget_category=True
    )
    db.add_all([income_cat, expense_cat, transfer_cat, budget_cat])
    db.flush()
    bills = None
    if with_bills:
        bills = SinkingFund(name="Bills", color="#FF0000", current_balance=500)
        db.add(bills)
        db.flush()
        db.add(
            RecurringBill(
                name="Rent",
                amount=1200,
                debtor_provider="Landlord",
                start_date="2026-01-01",
                frequency="monthly",
                category_id=expense_cat.id,
                next_due_date="2026-02-01",
            )
        )
    savings = SinkingFund(name="Savings", color="#00FF00", current_balance=1000)
    db.add(savings)
    db.flush()
    overflow = None
    allocation = IncomeAllocation(
        monthly_income_amount=Decimal(income),
        monthly_budget_allocation=800,
        bills_fund_allocation_type="recommended",
    )
    db.add(allocation)
    db.flush()
    db.add(
        IncomeAllocationToSinkingFund(
            income_allocation_id=allocation.id,
            sinking_fund_id=savings.id,
            allocation_amount=500,
        )
    )
    if with_overflow:
        overflow = SinkingFund(
            name="Short Term Savings",
            color="#AABBCC",
            current_balance=Decimal(overflow_balance),
        )
        db.add(overflow)
        db.flush()
        allocation.overflow_sinking_fund_id = overflow.id
    db.commit()
    return {
        "income_cat": income_cat,
        "transfer_cat": transfer_cat,
        "budget_cat": budget_cat,
        "bills": bills,
        "savings": savings,
        "overflow": overflow,
        "allocation": allocation,
    }


def _patch_today(monkeypatch, day: date):
    monkeypatch.setattr("app.tasks._today", lambda: day)


def _income_count(db) -> int:
    return (
        db.query(Transaction)
        .filter(Transaction.description.like("Monthly income %"))
        .count()
    )


class TestBootstrap:
    def test_rerun_does_not_change_balances_or_add_rows(self, db_session, monkeypatch):
        _patch_today(monkeypatch, date(2026, 11, 15))
        ready = _ready(db_session)
        surplus, _short = sweep_descriptions(10, 2026)
        db_session.add(
            Transaction(
                date="2026-11-01",
                description=surplus,
                amount=25,
                category_id=ready["transfer_cat"].id,
                type="transfer",
                transaction_type="contribution",
                sinking_fund_id=ready["savings"].id,
            )
        )
        db_session.commit()
        before = Decimal(str(ready["savings"].current_balance))
        ensure_backfill(db_session, today_value=date(2026, 11, 15))
        ensure_backfill(db_session, today_value=date(2026, 11, 15))
        db_session.refresh(ready["savings"])
        assert Decimal(str(ready["savings"].current_balance)) == before
        october = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 10)
            .one()
        )
        assert october.settlement_outcome == "surplus"
        assert october.settlement_fund_id == ready["savings"].id
        assert october.processed_at is None
        november = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 11)
            .one()
        )
        assert november.processed_at is not None
        assert db_session.query(HouseholdMonth).count() == 2

    def test_conflicting_sweep_funds_are_unresolved(self, db_session, monkeypatch):
        _patch_today(monkeypatch, date(2026, 11, 15))
        ready = _ready(db_session)
        other = SinkingFund(name="Other", color="#111111", current_balance=5)
        db_session.add(other)
        db_session.flush()
        surplus, _short = sweep_descriptions(10, 2026)
        for fund in (ready["savings"], other):
            db_session.add(
                Transaction(
                    date="2026-11-01",
                    description=surplus,
                    amount=10,
                    category_id=ready["transfer_cat"].id,
                    type="transfer",
                    transaction_type="contribution",
                    sinking_fund_id=fund.id,
                )
            )
        db_session.commit()
        ensure_backfill(db_session, today_value=date(2026, 11, 15))
        october = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 10)
            .one()
        )
        assert october.settlement_outcome == "unresolved"
        assert october.settlement_fund_id is None
        process_income_allocation(db=db_session)
        sweeps = (
            db_session.query(Transaction)
            .filter(Transaction.description == surplus)
            .count()
        )
        assert sweeps == 2

    def test_system_income_blocks_a_second_distribution(self, db_session, monkeypatch):
        _patch_today(monkeypatch, date(2026, 2, 1))
        ready = _ready(db_session)
        db_session.add(
            Transaction(
                date="2026-02-01",
                description=income_description(2, 2026),
                amount=5000,
                category_id=ready["income_cat"].id,
                type="expense",
                transaction_type="regular",
            )
        )
        db_session.commit()
        before = Decimal(str(ready["savings"].current_balance))
        process_income_allocation(db=db_session)
        db_session.refresh(ready["savings"])
        assert Decimal(str(ready["savings"].current_balance)) == before
        assert _income_count(db_session) == 1

    def test_stray_income_before_bootstrap_holds_the_upgrade_month(
        self, db_session, monkeypatch
    ):
        _patch_today(monkeypatch, date(2026, 2, 15))
        ready = _ready(db_session)
        db_session.add(
            Transaction(
                date="2026-02-10",
                description="Pay",
                amount=100,
                category_id=ready["income_cat"].id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        assert _income_count(db_session) == 0
        meta = db_session.get(HouseholdProcessing, 1)
        assert meta.held_year == 2026
        assert meta.held_month == 2

    def test_stray_income_after_bootstrap_does_not_block(self, db_session, monkeypatch):
        day = {"value": date(2026, 1, 15)}
        monkeypatch.setattr("app.tasks._today", lambda: day["value"])
        ready = _ready(db_session)
        ensure_backfill(db_session, today_value=date(2026, 1, 15))
        meta = db_session.get(HouseholdProcessing, 1)
        assert meta.held_month is None
        day["value"] = date(2026, 2, 15)
        db_session.add(
            Transaction(
                date="2026-02-03",
                description="Pay",
                amount=80,
                category_id=ready["income_cat"].id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        assert _income_count(db_session) == 1
        february = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 2)
            .one()
        )
        assert february.processed_at is not None
        assert db_session.get(HouseholdProcessing, 1).held_month is None


class TestClaims:
    def test_settlement_only_row_still_distributes(self, db_session, monkeypatch):
        _patch_today(monkeypatch, date(2026, 2, 1))
        ready = _ready(db_session, with_overflow=True)
        db_session.add(
            HouseholdMonth(
                year=2026,
                month=2,
                settlement_outcome="zero",
                settlement_fund_id=ready["overflow"].id,
                settlement_amount=0,
                settled_on="2026-03-01",
            )
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        row = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 2)
            .one()
        )
        assert row.processed_at is not None
        assert row.settlement_outcome == "zero"
        assert row.settlement_fund_id == ready["overflow"].id
        assert _income_count(db_session) == 1
        process_income_allocation(db=db_session)
        assert _income_count(db_session) == 1

    def test_processed_only_previous_month_is_settled_once(
        self, db_session, monkeypatch
    ):
        _patch_today(monkeypatch, date(2026, 2, 1))
        ready = _ready(db_session, with_overflow=True, overflow_balance="0")
        db_session.add(
            HouseholdMonth(
                year=2026,
                month=1,
                processed_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db_session.add(
            Budget(
                category_id=ready["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=500,
                spent_amount=300,
                fund_balance=0,
            )
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        process_income_allocation(db=db_session)
        january = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 1)
            .one()
        )
        assert january.processed_at is not None
        assert january.settlement_outcome == "surplus"
        assert january.settlement_fund_id == ready["overflow"].id
        sweeps = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "contribution")
            .all()
        )
        assert len(sweeps) == 1
        assert sweeps[0].description == sweep_descriptions(1, 2026)[0]
        db_session.refresh(ready["overflow"])
        assert Decimal(str(ready["overflow"].current_balance)) == Decimal(200)
        bills = (
            db_session.query(Transaction)
            .filter(Transaction.description == "Income allocation to Bills fund")
            .one()
        )
        assert Decimal(str(bills.amount)) == Decimal("1200.00")
        assert Decimal(str(sweeps[0].amount)) == Decimal(200)

    def test_missing_bills_fund_is_not_added_to_the_sweep(
        self, db_session, monkeypatch
    ):
        _patch_today(monkeypatch, date(2026, 2, 1))
        ready = _ready(db_session, with_bills=False, with_overflow=True)
        db_session.add(
            Budget(
                category_id=ready["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=500,
                spent_amount=300,
                fund_balance=0,
            )
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        sweep = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "contribution")
            .one()
        )
        assert Decimal(str(sweep.amount)) == Decimal(200)
        assert (
            db_session.query(Transaction)
            .filter(Transaction.description == "Income allocation to Bills fund")
            .count()
            == 0
        )

    def test_january_settles_december(self, db_session, monkeypatch):
        _patch_today(monkeypatch, date(2027, 1, 1))
        ready = _ready(db_session, with_overflow=True)
        db_session.add(
            Budget(
                category_id=ready["budget_cat"].id,
                month=12,
                year=2026,
                allocated_amount=40,
                spent_amount=10,
                fund_balance=0,
            )
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        december = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 12)
            .one()
        )
        assert december.settlement_outcome == "surplus"
        sweep = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "contribution")
            .one()
        )
        assert "December 2026" in sweep.description

    def test_zero_income_still_marks_processed(self, db_session, monkeypatch):
        _patch_today(monkeypatch, date(2026, 2, 1))
        _ready(db_session, income="0")
        process_income_allocation(db=db_session)
        income = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "income")
            .one()
        )
        assert Decimal(str(income.amount)) == Decimal(0)
        row = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 2)
            .one()
        )
        assert row.processed_at is not None
        january = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 1)
            .one()
        )
        assert january.settlement_outcome == "no_overflow"

    def test_zero_surplus_stores_the_fund_and_posts_no_sweep(
        self, db_session, monkeypatch
    ):
        _patch_today(monkeypatch, date(2026, 2, 1))
        ready = _ready(db_session, with_overflow=True)
        db_session.add(
            Budget(
                category_id=ready["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=500,
                spent_amount=500,
                fund_balance=0,
            )
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        january = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 1)
            .one()
        )
        assert january.settlement_outcome == "zero"
        assert january.settlement_fund_id == ready["overflow"].id
        assert (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "contribution")
            .count()
            == 0
        )

    def test_existing_unallocated_remainder_is_kept(self, db_session, monkeypatch):
        _patch_today(monkeypatch, date(2026, 2, 1))
        _ready(db_session)
        db_session.add(
            MonthlyUnallocatedIncome(month=2, year=2026, unallocated_amount=40)
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        row = (
            db_session.query(MonthlyUnallocatedIncome)
            .filter(
                MonthlyUnallocatedIncome.month == 2,
                MonthlyUnallocatedIncome.year == 2026,
            )
            .one()
        )
        assert Decimal(str(row.unallocated_amount)) == Decimal("2540.00")

    def test_deleting_the_income_row_does_not_distribute_again(
        self, db_session, monkeypatch
    ):
        _patch_today(monkeypatch, date(2026, 2, 1))
        ready = _ready(db_session)
        process_income_allocation(db=db_session)
        income = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "income")
            .one()
        )
        db_session.delete(income)
        db_session.commit()
        process_income_allocation(db=db_session)
        assert (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "income")
            .count()
            == 0
        )
        db_session.refresh(ready["savings"])
        assert Decimal(str(ready["savings"].current_balance)) == Decimal(1500)
        row = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 2)
            .one()
        )
        assert row.processed_at is not None

    def test_deleting_the_sweep_does_not_sweep_again(self, db_session, monkeypatch):
        _patch_today(monkeypatch, date(2026, 2, 1))
        ready = _ready(db_session, with_overflow=True)
        db_session.add(
            Budget(
                category_id=ready["budget_cat"].id,
                month=1,
                year=2026,
                allocated_amount=500,
                spent_amount=300,
                fund_balance=0,
            )
        )
        db_session.commit()
        process_income_allocation(db=db_session)
        sweep = (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "contribution")
            .one()
        )
        apply_transaction_balance_effects(
            db_session,
            sinking_fund_id=sweep.sinking_fund_id,
            txn_type=sweep.type,
            amount=sweep.amount,
            budget_id=None,
            transaction_type=sweep.transaction_type,
            reverse=True,
        )
        db_session.delete(sweep)
        db_session.commit()
        process_income_allocation(db=db_session)
        assert (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "contribution")
            .count()
            == 0
        )
        january = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 1)
            .one()
        )
        assert january.settlement_outcome == "surplus"
        assert january.settlement_fund_id == ready["overflow"].id

    def test_rollback_leaves_nothing_and_the_next_call_posts_once(
        self, db_session, monkeypatch
    ):
        _patch_today(monkeypatch, date(2026, 2, 1))
        ready = _ready(db_session)
        real_commit = db_session.commit
        calls = {"n": 0}

        def exploding_commit():
            calls["n"] += 1
            if calls["n"] >= 3:
                raise RuntimeError("injected")
            real_commit()

        monkeypatch.setattr(db_session, "commit", exploding_commit)
        with pytest.raises(RuntimeError, match="injected"):
            process_income_allocation(db=db_session)
        db_session.rollback()
        assert db_session.query(Transaction).count() == 0
        assert db_session.query(HouseholdMonth).count() == 0
        db_session.refresh(ready["savings"])
        assert Decimal(str(ready["savings"].current_balance)) == Decimal(1000)
        monkeypatch.undo()
        process_income_allocation(db=db_session)
        assert _income_count(db_session) == 1
        db_session.refresh(ready["savings"])
        assert Decimal(str(ready["savings"].current_balance)) == Decimal(1500)


class TestRecovery:
    def test_window_uses_localize_not_the_lmt_offset(self):
        closed = TIMEZONE.localize(datetime(2026, 11, 1, 0, 2))
        opened = TIMEZONE.localize(datetime(2026, 11, 1, 0, 5))
        assert month_window_open(closed) is False
        assert month_window_open(opened) is True
        assert (
            month_window_open(TIMEZONE.localize(datetime(2026, 11, 15, 12, 0))) is True
        )

    def test_first_install_and_attempt_flag(self, db_session):
        early = TIMEZONE.localize(datetime(2026, 11, 1, 0, 2))
        opened = TIMEZONE.localize(datetime(2026, 11, 1, 0, 5))
        later = TIMEZONE.localize(datetime(2026, 11, 2, 9, 0))
        mid = TIMEZONE.localize(datetime(2026, 11, 15, 9, 0))
        assert automatic_recovery_allowed(db_session, early) is False
        assert automatic_recovery_allowed(db_session, opened) is True
        assert automatic_recovery_allowed(db_session, later) is False
        assert automatic_recovery_allowed(db_session, mid) is False
        db_session.add(HouseholdProcessing(id=1, attempt_year=2026, attempt_month=11))
        db_session.commit()
        assert automatic_recovery_allowed(db_session, later) is True

    def test_missing_category_is_retried_later_in_the_same_month(
        self, db_session, monkeypatch
    ):
        _patch_today(monkeypatch, date(2026, 11, 1))
        process_income_allocation(db=db_session)
        assert db_session.query(Transaction).count() == 0
        meta = db_session.get(HouseholdProcessing, 1)
        assert meta.attempt_year == 2026
        assert meta.attempt_month == 11
        later = TIMEZONE.localize(datetime(2026, 11, 20, 8, 0))
        assert automatic_recovery_allowed(db_session, later) is True
        _ready(db_session)
        monkeypatch.setattr(
            "app.services.household_months.now",
            lambda: later,
        )
        recover_current_month()
        assert _income_count(db_session) == 1

    def test_startup_before_0005_posts_nothing(self, db_session, monkeypatch):
        moment = TIMEZONE.localize(datetime(2026, 11, 1, 0, 2))
        monkeypatch.setattr("app.services.household_months.now", lambda: moment)
        _patch_today(monkeypatch, date(2026, 11, 1))
        _ready(db_session)
        recover_current_month()
        assert db_session.query(Transaction).count() == 0

    def test_empty_database_on_the_first_after_0005_runs_once(
        self, db_session, monkeypatch
    ):
        moment = TIMEZONE.localize(datetime(2026, 11, 1, 0, 6))
        monkeypatch.setattr("app.services.household_months.now", lambda: moment)
        _patch_today(monkeypatch, date(2026, 11, 1))
        _ready(db_session)
        recover_current_month()
        recover_current_month()
        assert _income_count(db_session) == 1

    def test_empty_database_mid_month_does_not_run(self, db_session, monkeypatch):
        moment = TIMEZONE.localize(datetime(2026, 11, 15, 9, 0))
        monkeypatch.setattr("app.services.household_months.now", lambda: moment)
        _patch_today(monkeypatch, date(2026, 11, 15))
        _ready(db_session)
        recover_current_month()
        assert db_session.query(Transaction).count() == 0

    def test_several_missed_months_post_only_the_current_one(
        self, db_session, monkeypatch
    ):
        ready = _ready(db_session)
        db_session.add(
            Transaction(
                date="2026-07-01",
                description=income_description(7, 2026),
                amount=1,
                category_id=ready["income_cat"].id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()
        moment = TIMEZONE.localize(datetime(2026, 11, 15, 9, 0))
        monkeypatch.setattr("app.services.household_months.now", lambda: moment)
        _patch_today(monkeypatch, date(2026, 11, 15))
        recover_current_month()
        descriptions = [row.description for row in db_session.query(Transaction).all()]
        assert income_description(11, 2026) in descriptions
        assert income_description(9, 2026) not in descriptions
        assert income_description(10, 2026) not in descriptions
        september = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 9)
            .first()
        )
        assert september is None
        october = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 10)
            .one()
        )
        assert october.processed_at is None
        assert october.settlement_outcome == "no_overflow"
        july = (
            db_session.query(HouseholdMonth)
            .filter(HouseholdMonth.year == 2026, HouseholdMonth.month == 7)
            .one()
        )
        assert july.processed_at is not None

    def test_held_month_is_not_recovered(self, db_session, monkeypatch):
        moment = TIMEZONE.localize(datetime(2026, 11, 15, 9, 0))
        monkeypatch.setattr("app.services.household_months.now", lambda: moment)
        _patch_today(monkeypatch, date(2026, 11, 15))
        ready = _ready(db_session)
        db_session.add(
            Transaction(
                date="2026-07-01",
                description=income_description(7, 2026),
                amount=1,
                category_id=ready["income_cat"].id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.add(
            Transaction(
                date="2026-11-02",
                description="Pay",
                amount=20,
                category_id=ready["income_cat"].id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()
        recover_current_month()
        assert income_description(11, 2026) not in [
            row.description for row in db_session.query(Transaction).all()
        ]
        meta = db_session.get(HouseholdProcessing, 1)
        assert meta.held_year == 2026
        assert meta.held_month == 11

    def test_scheduler_registers_income_recovery(self):
        from app.scheduler import register_jobs

        calls: list[dict] = []

        class _Recorder:
            def add_job(self, *args, **kwargs):
                calls.append(kwargs)

        register_jobs(_Recorder())
        by_id = {call["id"]: call for call in calls}
        income = by_id["income_allocation"]
        assert income["max_instances"] == 1
        assert income["coalesce"] is True
        assert income["misfire_grace_time"] is None
        retry = by_id["income_allocation_retry"]
        assert retry["max_instances"] == 1
        assert retry["coalesce"] is True
        assert retry["misfire_grace_time"] is None
        assert "income_allocation_catchup" in by_id
        bill = by_id["bill_processing"]
        assert bill["misfire_grace_time"] is None
        assert bill["coalesce"] is True
        assert bill["max_instances"] == 1
        catchup = by_id["bill_processing_catchup"]
        assert catchup["max_instances"] == 1

    def test_bill_catchup_runs_about_three_minutes_after_boot(self, monkeypatch):
        from datetime import datetime, timedelta

        from app.config import TIMEZONE
        from app.scheduler import register_jobs
        from app.tasks import process_due_bills

        monkeypatch.setattr("app.config.SHARES_CATCHUP_ON_STARTUP", False)
        recorded: list[tuple] = []

        class _Recorder:
            def add_job(self, func, trigger, **kwargs):
                recorded.append((func, trigger, kwargs))

        before = datetime.now(TIMEZONE)
        register_jobs(_Recorder())
        after = datetime.now(TIMEZONE)
        by_id = {
            kwargs["id"]: (func, trigger, kwargs) for func, trigger, kwargs in recorded
        }
        func, trigger, kwargs = by_id["bill_processing_catchup"]
        assert func is process_due_bills
        assert kwargs["max_instances"] == 1
        run_at = trigger.run_date
        earliest = before + timedelta(minutes=3) - timedelta(seconds=2)
        latest = after + timedelta(minutes=3) + timedelta(seconds=2)
        assert earliest <= run_at <= latest
        cron_func, _cron_trigger, cron_kwargs = by_id["bill_processing"]
        assert cron_func is process_due_bills
        assert cron_kwargs["misfire_grace_time"] is None
        assert cron_kwargs["coalesce"] is True
        assert cron_kwargs["max_instances"] == 1
        assert "share_market_data_catchup" not in by_id


class TestOperatorScript:
    def test_dry_run_warns_and_posts_nothing(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "scripts.review_monthly_processing.today", lambda: date(2026, 2, 15)
        )
        _patch_today(monkeypatch, date(2026, 2, 15))
        ready = _ready(db_session)
        db_session.add(
            Transaction(
                date="2026-02-02",
                description="Pay",
                amount=10,
                category_id=ready["income_cat"].id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()
        text_report = report(db_session)
        assert "Upgrade hold: yes" in text_report
        assert "paused" in text_report
        assert "double-post" in text_report
        assert _income_count(db_session) == 0

    def test_apply_posts_a_held_month_once(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "scripts.review_monthly_processing.today", lambda: date(2026, 2, 15)
        )
        _patch_today(monkeypatch, date(2026, 2, 15))
        ready = _ready(db_session)
        db_session.add(
            Transaction(
                date="2026-02-02",
                description="Pay",
                amount=10,
                category_id=ready["income_cat"].id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()
        assert apply_current_month(db_session) == 0
        assert _income_count(db_session) == 1
        assert apply_current_month(db_session) == 0
        assert _income_count(db_session) == 1
        assert db_session.get(HouseholdProcessing, 1).held_month is None


def test_migration_creates_household_months_and_a_blank_bootstrap_row(tmp_path):
    url = f"sqlite:///{tmp_path / 'migrate.sqlite'}"
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    env["SECRET_KEY"] = "migration-test-secret-key-not-for-production-use"
    root = Path(__file__).resolve().parents[1]

    def alembic(*args: str) -> None:
        result = subprocess.run(
            ["uv", "run", "alembic", *args],
            check=False,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    alembic("upgrade", "head")
    engine = create_engine(url)
    assert "household_months" in inspect(engine).get_table_names()
    session = sessionmaker(bind=engine)()
    meta = session.get(HouseholdProcessing, 1)
    assert meta is not None
    assert meta.backfilled_at is None
    session.close()
    alembic("downgrade", "k1l2m3n4o5p6")
    assert "household_months" not in inspect(engine).get_table_names()
    engine.dispose()


class TestDialectClaims:
    def test_postgresql_and_sqlite_claims_are_conditional_updates(self):
        moment = datetime(2026, 11, 1, tzinfo=UTC)
        for dialect_name, dialect in (
            (
                "postgresql",
                __import__(
                    "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
                ).dialect(),
            ),
            (
                "sqlite",
                __import__(
                    "sqlalchemy.dialects.sqlite", fromlist=["dialect"]
                ).dialect(),
            ),
        ):
            processed = str(
                processed_claim_statement(dialect_name, 2026, 11, moment).compile(
                    dialect=dialect
                )
            )
            settlement = str(
                settlement_claim_statement(
                    dialect_name,
                    2026,
                    10,
                    "zero",
                    3,
                    Decimal("0.00"),
                    "2026-11-01",
                ).compile(dialect=dialect)
            )
            assert "ON CONFLICT" in processed
            assert "processed_at IS NULL" in processed
            assert "ON CONFLICT" in settlement
            assert "settlement_outcome IS NULL" in settlement

    def test_two_connections_post_one_distribution(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.tasks._today", lambda: date(2026, 2, 1))
        url = f"sqlite:///{tmp_path / 'claims.sqlite'}"
        engine = create_engine(url, connect_args={"timeout": 30}, poolclass=NullPool)
        with engine.begin() as conn:
            conn.execute(text("PRAGMA busy_timeout=30000"))
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
        seed = factory()
        try:
            _ready(seed, with_overflow=True)
        finally:
            seed.close()
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _run():
            local = factory()
            try:
                barrier.wait(timeout=10)
                process_income_allocation(db=local)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                local.close()

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        check = factory()
        try:
            assert _income_count(check) == 1
            assert (
                check.query(HouseholdMonth)
                .filter(
                    HouseholdMonth.year == 2026,
                    HouseholdMonth.month == 2,
                    HouseholdMonth.processed_at.is_not(None),
                )
                .count()
                == 1
            )
            assert (
                check.query(Transaction)
                .filter(Transaction.transaction_type == "contribution")
                .count()
                == 0
            )
        finally:
            check.close()
            engine.dispose()
