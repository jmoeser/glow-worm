"""Confirm, skip, and paid-from rules for recurring payments."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm.attributes import set_committed_value

from app.models import (
    Budget,
    Category,
    HouseholdMonth,
    IncomeAllocation,
    RecurringBill,
    SinkingFund,
    Transaction,
)
from app.services.recurring_payments import (
    PaidFromError,
    PaymentBlocked,
    PaymentConflict,
    apply_paid_from,
    confirm_payment,
    due_to_confirm,
    pay_bills_fund,
    skip_payment,
)
from app.tasks import advance_due_date

TODAY = date(2026, 11, 3)


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr("app.services.recurring_payments._today", lambda: TODAY)
    return TODAY


@pytest.fixture
def household(db_session):
    bills_cat = Category(name="Bills", type="expense", color="#111111", is_system=True)
    groceries = Category(
        name="Groceries", type="expense", color="#22C55E", is_budget_category=True
    )
    transfer = Category(
        name="Transfer", type="transfer", color="#888888", is_system=True
    )
    income = Category(name="Income", type="income", color="#00aa00", is_system=True)
    db_session.add_all([bills_cat, groceries, transfer, income])
    db_session.flush()
    bills = SinkingFund(
        name="Bills", color="#ff0000", current_balance=500, is_system=True
    )
    kids = SinkingFund(name="Kids fund", color="#00ff00", current_balance=40)
    overflow = SinkingFund(name="Overflow", color="#0000ff", current_balance=10)
    db_session.add_all([bills, kids, overflow])
    db_session.flush()
    october = Budget(
        category_id=groceries.id,
        month=10,
        year=2026,
        allocated_amount=400,
        spent_amount=0,
        fund_balance=0,
    )
    november = Budget(
        category_id=groceries.id,
        month=11,
        year=2026,
        allocated_amount=400,
        spent_amount=0,
        fund_balance=0,
    )
    db_session.add_all([october, november])
    db_session.commit()
    return {
        "bills_cat": bills_cat,
        "groceries": groceries,
        "transfer": transfer,
        "income": income,
        "bills": bills,
        "kids": kids,
        "overflow": overflow,
        "october": october,
        "november": november,
    }


def _bill(db, household, **kwargs):
    values = {
        "name": "HelloFresh",
        "amount": Decimal("89.95"),
        "debtor_provider": "HelloFresh",
        "start_date": "2026-01-01",
        "frequency": "weekly",
        "category_id": household["groceries"].id,
        "next_due_date": "2026-10-26",
        "paid_from": "budget",
        "bill_type": "fixed",
    }
    values.update(kwargs)
    bill = RecurringBill(**values)
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return bill


def _income_in(db, household, when: str):
    db.add(
        Transaction(
            date=when,
            description="Monthly income",
            amount=1000,
            category_id=household["income"].id,
            type="income",
            transaction_type="income",
        )
    )
    db.commit()


def _set_overflow(db, household, fund=None, deleted=False):
    target = household["overflow"] if fund is None else fund
    if deleted:
        target.is_deleted = True
    alloc = IncomeAllocation(
        monthly_income_amount=5000,
        monthly_budget_allocation=400,
        bills_fund_allocation_type="fixed",
        bills_fund_fixed_amount=0,
        overflow_sinking_fund_id=None if fund is False else target.id,
    )
    db.add(alloc)
    db.commit()
    return alloc


class TestPaidFromValidation:
    def test_budget_must_be_a_live_budget_category(self, db_session, household):
        bill = _bill(db_session, household)
        household["groceries"].is_deleted = True
        db_session.commit()
        with pytest.raises(PaidFromError, match="budget category"):
            apply_paid_from(
                db_session,
                bill,
                paid_from="budget",
                category_id=household["groceries"].id,
                force=True,
            )

    def test_fund_cannot_be_the_bills_fund_or_deleted(self, db_session, household):
        bill = _bill(
            db_session,
            household,
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
        )
        with pytest.raises(PaidFromError, match="choose Bills fund"):
            apply_paid_from(
                db_session,
                bill,
                paid_from="sinking_fund",
                paid_from_fund_id=household["bills"].id,
                category_id=household["bills_cat"].id,
            )
        household["kids"].is_deleted = True
        db_session.commit()
        with pytest.raises(PaidFromError, match="deleted"):
            apply_paid_from(
                db_session,
                bill,
                paid_from="sinking_fund",
                paid_from_fund_id=household["kids"].id,
                category_id=household["bills_cat"].id,
            )

    def test_switching_away_clears_the_fund_id(self, db_session, household):
        bill = _bill(
            db_session,
            household,
            paid_from="sinking_fund",
            paid_from_fund_id=household["kids"].id,
            category_id=household["bills_cat"].id,
        )
        apply_paid_from(
            db_session,
            bill,
            paid_from="budget",
            paid_from_fund_id=household["kids"].id,
            category_id=household["groceries"].id,
        )
        db_session.commit()
        db_session.refresh(bill)
        assert bill.paid_from == "budget"
        assert bill.paid_from_fund_id is None

    def test_rename_still_saves_when_the_fund_was_deleted(self, db_session, household):
        bill = _bill(
            db_session,
            household,
            paid_from="sinking_fund",
            paid_from_fund_id=household["kids"].id,
            category_id=household["bills_cat"].id,
        )
        household["kids"].is_deleted = True
        bill.name = "Child care"
        db_session.commit()
        db_session.refresh(bill)
        assert bill.name == "Child care"
        assert bill.paid_from_fund_id == household["kids"].id


class TestConfirmBudget:
    def test_charges_the_due_month_and_advances(
        self, db_session, household, fixed_today
    ):
        bill = _bill(db_session, household, next_due_date="2026-10-26")
        result = confirm_payment(db_session, bill, Decimal("92.40"), "2026-10-26")
        db_session.commit()
        txn = result.transaction
        assert txn.transaction_type == "budget_expense"
        assert txn.budget_id == household["october"].id
        assert txn.category_id == household["groceries"].id
        assert txn.date == "2026-10-26"
        assert txn.recurring_bill_id == bill.id
        assert txn.description == "Bill payment: HelloFresh to HelloFresh"
        db_session.refresh(household["october"])
        assert Decimal(str(household["october"].spent_amount)) == Decimal("92.40")
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-11-02"
        assert result.taken_back is None
        assert "October 2026" in result.message
        assert "Groceries" in result.message

    def test_later_month_still_charges_the_due_month(
        self, db_session, household, fixed_today
    ):
        bill = _bill(db_session, household, next_due_date="2026-10-05")
        confirm_payment(db_session, bill, Decimal(10), "2026-10-05")
        db_session.commit()
        db_session.refresh(household["october"])
        db_session.refresh(household["november"])
        assert Decimal(str(household["october"].spent_amount)) == Decimal("10.00")
        assert Decimal(str(household["november"].spent_amount)) == Decimal("0.00")


class TestOverflowTakeBack:
    def _swept(self, db, household, sweep=None):
        _set_overflow(db, household)
        _income_in(db, household, "2026-11-01")
        if sweep == "surplus":
            db.add(
                Transaction(
                    date="2026-11-01",
                    description="Budget surplus sweep — October 2026",
                    amount=20,
                    category_id=household["transfer"].id,
                    type="transfer",
                    transaction_type="contribution",
                    sinking_fund_id=household["overflow"].id,
                )
            )
            db.add(
                HouseholdMonth(
                    year=2026,
                    month=10,
                    settlement_outcome="surplus",
                    settlement_fund_id=household["overflow"].id,
                    settlement_amount=20,
                    settled_on="2026-11-01",
                )
            )
        elif sweep == "shortfall":
            db.add(
                Transaction(
                    date="2026-11-01",
                    description="Budget shortfall — October 2026",
                    amount=15,
                    category_id=household["transfer"].id,
                    type="expense",
                    transaction_type="withdrawal",
                    sinking_fund_id=household["overflow"].id,
                )
            )
            db.add(
                HouseholdMonth(
                    year=2026,
                    month=10,
                    settlement_outcome="shortfall",
                    settlement_fund_id=household["overflow"].id,
                    settlement_amount=15,
                    settled_on="2026-11-01",
                )
            )
        db.commit()

    @pytest.mark.parametrize("sweep", ["surplus", "shortfall"])
    def test_takes_the_amount_back(self, db_session, household, fixed_today, sweep):
        self._swept(db_session, household, sweep)
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is not None
        assert result.taken_back["fund_name"] == "Overflow"
        assert result.taken_back["amount"] == 90.0
        assert "October" in result.message
        assert "Overflow" in result.message
        back = db_session.get(Transaction, result.taken_back["transaction_id"])
        assert back.transaction_type == "withdrawal"
        assert back.type == "expense"
        assert back.date == "2026-11-03"
        assert back.category_id == household["transfer"].id
        assert "Taken back for HelloFresh (October 2026 Groceries)" == back.description
        db_session.refresh(household["overflow"])
        assert Decimal(str(household["overflow"].current_balance)) == Decimal("-80.00")

    def test_old_month_without_a_sweep_records_the_payment_and_does_not_debit(
        self, db_session, household, fixed_today
    ):
        self._swept(db_session, household, None)
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is None
        assert result.transaction.transaction_type == "budget_expense"
        assert (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "withdrawal")
            .count()
            == 0
        )
        db_session.refresh(household["overflow"])
        assert Decimal(str(household["overflow"].current_balance)) == Decimal("10.00")

    def test_zero_outcome_debits_the_recorded_fund_after_the_setting_changes(
        self, db_session, household, fixed_today
    ):
        other = SinkingFund(name="Later", color="#abcdef", current_balance=70)
        db_session.add(other)
        db_session.flush()
        _set_overflow(db_session, household, fund=other)
        db_session.add(
            HouseholdMonth(
                year=2026,
                month=10,
                settlement_outcome="zero",
                settlement_fund_id=household["overflow"].id,
                settlement_amount=0,
                settled_on="2026-11-01",
            )
        )
        db_session.commit()
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is not None
        assert result.taken_back["fund_id"] == household["overflow"].id
        db_session.refresh(household["overflow"])
        db_session.refresh(other)
        assert Decimal(str(household["overflow"].current_balance)) == Decimal("-80.00")
        assert Decimal(str(other.current_balance)) == Decimal("70.00")

    def test_no_overflow_outcome_does_not_debit_after_a_fund_is_configured(
        self, db_session, household, fixed_today
    ):
        _set_overflow(db_session, household)
        db_session.add(
            HouseholdMonth(
                year=2026,
                month=10,
                settlement_outcome="no_overflow",
                settlement_amount=0,
                settled_on="2026-11-01",
            )
        )
        db_session.commit()
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is None
        db_session.refresh(household["overflow"])
        assert Decimal(str(household["overflow"].current_balance)) == Decimal("10.00")

    def test_deleted_stored_fund_skips_the_take_back(
        self, db_session, household, fixed_today
    ):
        household["overflow"].is_deleted = True
        db_session.add(
            HouseholdMonth(
                year=2026,
                month=10,
                settlement_outcome="surplus",
                settlement_fund_id=household["overflow"].id,
                settlement_amount=20,
                settled_on="2026-11-01",
            )
        )
        db_session.commit()
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is None

    def test_unresolved_sweeps_do_not_debit(self, db_session, household, fixed_today):
        db_session.add(
            HouseholdMonth(
                year=2026,
                month=10,
                settlement_outcome="unresolved",
                settled_on="2026-11-01",
            )
        )
        db_session.commit()
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is None

    def test_stray_income_in_the_next_month_does_not_claw_back(
        self, db_session, household, fixed_today
    ):
        _set_overflow(db_session, household)
        _income_in(db_session, household, "2026-11-02")
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is None
        payment = result.transaction
        assert payment.transaction_type == "budget_expense"

    def test_no_overflow_fund_skips_the_take_back(
        self, db_session, household, fixed_today
    ):
        _income_in(db_session, household, "2026-11-01")
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is None
        assert (
            db_session.query(Transaction)
            .filter(Transaction.transaction_type == "withdrawal")
            .count()
            == 0
        )

    def test_deleted_overflow_fund_skips_the_take_back(
        self, db_session, household, fixed_today
    ):
        _set_overflow(db_session, household, deleted=True)
        _income_in(db_session, household, "2026-11-01")
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        assert result.taken_back is None

    def test_unswept_month_skips_the_take_back(
        self, db_session, household, fixed_today
    ):
        _set_overflow(db_session, household)
        bill = _bill(db_session, household, next_due_date="2026-11-02")
        result = confirm_payment(db_session, bill, Decimal(12), "2026-11-02")
        db_session.commit()
        assert result.taken_back is None

    def test_stale_or_blocked_writes_no_take_back(
        self, db_session, household, fixed_today
    ):
        self._swept(db_session, household, "surplus")
        bill = _bill(db_session, household, next_due_date="2026-09-01")
        with pytest.raises(PaymentBlocked) as blocked:
            confirm_payment(db_session, bill, Decimal(10), "2026-09-01")
        assert blocked.value.code == "no_budget"
        db_session.rollback()
        rows = db_session.query(Transaction).all()
        assert all(row.transaction_type != "budget_expense" for row in rows)
        assert all(not (row.description or "").startswith("Taken back") for row in rows)
        bill = _bill(db_session, household)
        confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        db_session.commit()
        with pytest.raises(PaymentConflict) as stale:
            confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        assert stale.value.code == "stale"
        db_session.rollback()
        assert (
            db_session.query(Transaction)
            .filter(Transaction.description.like("Taken back%"))
            .count()
            == 1
        )

    def test_deleting_the_confirm_restores_spent_and_leaves_the_take_back(
        self, authed_client, db_session, household, fixed_today
    ):
        self._swept(db_session, household, "surplus")
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        # Legacy take-back: no link, so deleting the payment leaves it.
        clawback = db_session.get(Transaction, result.taken_back["transaction_id"])
        clawback.adjustment_for_transaction_id = None
        db_session.commit()
        deleted = authed_client.delete(f"/api/transactions/{result.transaction.id}")
        assert deleted.status_code == 200
        db_session.refresh(household["october"])
        db_session.refresh(household["overflow"])
        assert Decimal(str(household["october"].spent_amount)) == Decimal("0.00")
        assert Decimal(str(household["overflow"].current_balance)) == Decimal("-80.00")
        assert (
            db_session.get(Transaction, result.taken_back["transaction_id"]) is not None
        )


class TestConfirmFund:
    def test_lowers_the_balance_once_and_delete_restores_it(
        self, authed_client, db_session, household, fixed_today
    ):
        bill = _bill(
            db_session,
            household,
            name="Child care",
            paid_from="sinking_fund",
            paid_from_fund_id=household["kids"].id,
            category_id=household["bills_cat"].id,
            amount=Decimal(30),
        )
        result = confirm_payment(db_session, bill, Decimal(50), "2026-10-26")
        db_session.commit()
        txn = result.transaction
        assert txn.transaction_type == "regular"
        assert txn.sinking_fund_id == household["kids"].id
        assert txn.category_id == household["bills_cat"].id
        assert txn.description == "Bill payment: Child care to HelloFresh"
        db_session.refresh(household["kids"])
        assert Decimal(str(household["kids"].current_balance)) == Decimal("-10.00")
        authed_client.delete(f"/api/transactions/{txn.id}")
        db_session.refresh(household["kids"])
        assert Decimal(str(household["kids"].current_balance)) == Decimal("40.00")


class TestBlocksAndIdempotency:
    def test_error_body_and_blocks_leave_the_date(
        self, authed_client, db_session, household, fixed_today
    ):
        missing = _bill(db_session, household, next_due_date="2026-09-07")
        response = authed_client.post(
            f"/api/bills/{missing.id}/confirm",
            json={"amount": "10.00", "due_date": "2026-09-07"},
        )
        assert response.status_code == 400
        body = response.json()
        assert isinstance(body["detail"], str)
        assert body["code"] == "no_budget"
        assert "Groceries" in body["detail"]
        assert "September 2026" in body["detail"]
        db_session.refresh(missing)
        assert missing.next_due_date == "2026-09-07"
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == missing.id)
            .count()
            == 0
        )

        household["kids"].is_deleted = True
        db_session.commit()
        fund_bill = _bill(
            db_session,
            household,
            paid_from="sinking_fund",
            paid_from_fund_id=household["kids"].id,
            category_id=household["bills_cat"].id,
        )
        gone = authed_client.post(
            f"/api/bills/{fund_bill.id}/confirm",
            json={"amount": "5", "due_date": "2026-10-26"},
        )
        assert gone.status_code == 400
        assert gone.json()["code"] == "source_missing"
        assert "Kids fund was deleted" in gone.json()["detail"]

        household["groceries"].is_budget_category = False
        db_session.commit()
        unticked = _bill(db_session, household, next_due_date="2026-10-19")
        not_budget = authed_client.post(
            f"/api/bills/{unticked.id}/confirm",
            json={"amount": "5", "due_date": "2026-10-19"},
        )
        assert not_budget.status_code == 400
        assert not_budget.json()["code"] == "not_budget"

    def test_same_due_date_twice_is_one_transaction(
        self, db_session, household, fixed_today
    ):
        bill = _bill(db_session, household)
        confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        with pytest.raises(PaymentConflict) as exc:
            confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        assert exc.value.code == "stale"
        db_session.commit()
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .count()
            == 1
        )

    def test_wrong_due_date_and_inactive_and_fixed_bills_fund(
        self, db_session, household, fixed_today
    ):
        bill = _bill(db_session, household)
        with pytest.raises(PaymentConflict) as wrong:
            confirm_payment(db_session, bill, Decimal(10), "2026-10-19")
        assert wrong.value.code == "stale"
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-10-26"

        bill.is_active = False
        db_session.commit()
        with pytest.raises(PaymentConflict) as inactive:
            confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        assert inactive.value.code == "inactive"

        fixed = _bill(
            db_session,
            household,
            name="Rent",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            frequency="monthly",
        )
        with pytest.raises(PaymentConflict) as auto:
            confirm_payment(db_session, fixed, Decimal(10), fixed.next_due_date)
        assert auto.value.code == "wrong_source"
        db_session.refresh(fixed)
        assert fixed.next_due_date == "2026-10-26"

    def test_failed_update_does_not_write_the_old_date_back(
        self, db_session, household, fixed_today
    ):
        bill = _bill(db_session, household)
        confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        db_session.commit()
        db_session.refresh(bill)
        advanced = bill.next_due_date
        bill.next_due_date = "2026-10-26"
        with pytest.raises(PaymentConflict):
            confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        db_session.commit()
        db_session.refresh(bill)
        assert bill.next_due_date == advanced

    def test_confirm_then_pay_with_the_old_due_date_is_stale(
        self, db_session, household, fixed_today
    ):
        bill = _bill(
            db_session,
            household,
            name="Water",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            bill_type="variable",
            amount=Decimal(20),
        )
        confirm_payment(db_session, bill, Decimal(20), "2026-10-26")
        db_session.commit()
        db_session.refresh(bill)
        set_committed_value(bill, "next_due_date", "2026-10-26")
        with pytest.raises(PaymentConflict) as exc:
            pay_bills_fund(db_session, bill, Decimal(20), "2026-11-03")
        assert exc.value.code == "stale"
        db_session.commit()
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .count()
            == 1
        )

    def test_pay_then_confirm_is_stale(
        self, authed_client, db_session, household, fixed_today
    ):
        bill = _bill(
            db_session,
            household,
            name="Water",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            bill_type="variable",
        )
        paid = authed_client.post(
            f"/api/bills/{bill.id}/pay",
            json={"amount": "15.00", "date": "2026-11-03"},
        )
        assert paid.status_code == 200
        again = authed_client.post(
            f"/api/bills/{bill.id}/confirm",
            json={"amount": "15.00", "due_date": "2026-10-26"},
        )
        assert again.status_code == 409
        assert again.json()["code"] == "stale"
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .count()
            == 1
        )


class TestSkipAndDueList:
    def test_skip_advances_with_no_transaction_and_logs(
        self, db_session, household, fixed_today, caplog
    ):
        bill = _bill(db_session, household, next_due_date="2026-11-09")
        with caplog.at_level("INFO"):
            result = skip_payment(db_session, bill, "2026-11-09")
        db_session.commit()
        assert "2026-11-09" in caplog.text
        assert str(bill.id) in caplog.text
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-11-16"
        assert "16 Nov 2026" in result.message
        assert db_session.query(Transaction).count() == 0

    def test_skip_works_with_a_broken_source_and_early(
        self, db_session, household, fixed_today
    ):
        household["kids"].is_deleted = True
        db_session.commit()
        bill = _bill(
            db_session,
            household,
            paid_from="sinking_fund",
            paid_from_fund_id=household["kids"].id,
            category_id=household["bills_cat"].id,
            next_due_date="2026-12-01",
        )
        skip_payment(db_session, bill, "2026-12-01")
        db_session.commit()
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-12-08"

    def test_end_date_stops_the_list_and_confirm(
        self, db_session, household, fixed_today
    ):
        bill = _bill(
            db_session,
            household,
            next_due_date="2026-10-26",
            end_date="2026-10-20",
            frequency="weekly",
        )
        assert due_to_confirm(db_session, TODAY) == []
        with pytest.raises(PaymentConflict) as exc:
            confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        assert exc.value.code == "ended"
        assert "20 Oct 2026" in exc.value.detail
        with pytest.raises(PaymentConflict) as skipped:
            skip_payment(db_session, bill, "2026-10-26")
        assert skipped.value.code == "ended"

    def test_three_weekly_steps_and_the_due_list(
        self, db_session, household, fixed_today
    ):
        old = _bill(
            db_session, household, next_due_date="2026-10-20", name="HelloFresh"
        )
        _bill(
            db_session,
            household,
            name="Later",
            next_due_date="2026-11-02",
            frequency="weekly",
        )
        _bill(
            db_session,
            household,
            name="Rent",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            next_due_date="2026-10-01",
            frequency="monthly",
            bill_type="fixed",
        )
        _bill(
            db_session,
            household,
            name="Paused",
            next_due_date="2026-10-01",
            is_active=False,
        )
        _bill(
            db_session,
            household,
            name="Future",
            next_due_date="2026-12-01",
        )
        rows = due_to_confirm(db_session, TODAY)
        assert [row["name"] for row in rows] == ["HelloFresh", "Later"]
        assert rows[0]["more_due"] == 2
        assert rows[0]["due_date"] == "2026-10-20"
        confirm_payment(db_session, old, Decimal(10), old.next_due_date)
        db_session.commit()
        db_session.refresh(old)
        skip_payment(db_session, old, old.next_due_date)
        db_session.commit()
        db_session.refresh(old)
        confirm_payment(db_session, old, Decimal(10), old.next_due_date)
        db_session.commit()
        db_session.refresh(old)
        assert (
            old.next_due_date
            == advance_due_date(
                advance_due_date(
                    advance_due_date(date(2026, 10, 20), "weekly"), "weekly"
                ),
                "weekly",
            ).isoformat()
        )


class TestVariableBillsFund:
    def test_confirm_and_skip_and_pay_share_one_occurrence(
        self, authed_client, db_session, household, fixed_today
    ):
        bill = _bill(
            db_session,
            household,
            name="Water",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            bill_type="variable",
            amount=Decimal(210),
            frequency="monthly",
            next_due_date="2026-10-15",
        )
        listed = due_to_confirm(db_session, TODAY)
        assert listed[0]["name"] == "Water"
        assert listed[0]["paid_from_label"] == "Bills fund"
        result = confirm_payment(db_session, bill, Decimal("214.30"), "2026-10-15")
        db_session.commit()
        assert result.transaction.transaction_type == "regular"
        assert result.transaction.sinking_fund_id == household["bills"].id
        assert result.transaction.date == "2026-10-15"
        db_session.refresh(household["bills"])
        assert Decimal(str(household["bills"].current_balance)) == Decimal("285.70")
        db_session.refresh(bill)
        stepped = bill.next_due_date
        skip_payment(db_session, bill, stepped)
        db_session.commit()
        db_session.refresh(bill)
        assert bill.next_due_date != stepped

        other = _bill(
            db_session,
            household,
            name="Gas",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            bill_type="variable",
            next_due_date="2026-10-20",
        )
        paid = authed_client.post(
            f"/api/bills/{other.id}/pay",
            json={"amount": "40", "date": "2026-11-01"},
        )
        assert paid.status_code == 200
        second = authed_client.post(
            f"/api/bills/{other.id}/confirm",
            json={"amount": "40", "due_date": "2026-10-20"},
        )
        assert second.status_code == 409
        assert second.json()["code"] == "stale"
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == other.id)
            .count()
            == 1
        )

    def test_fixed_bills_fund_is_not_due(self, db_session, household, fixed_today):
        _bill(
            db_session,
            household,
            name="Rent",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            bill_type="fixed",
        )
        assert due_to_confirm(db_session, TODAY) == []


class TestApiShapes:
    def test_create_without_paid_from_defaults_and_due_route_is_not_a_bill_id(
        self, authed_client, db_session, household
    ):
        created = authed_client.post(
            "/api/bills",
            json={
                "name": "Netflix",
                "amount": "15.99",
                "debtor_provider": "Netflix",
                "start_date": "2026-01-01",
                "frequency": "monthly",
                "category_id": household["bills_cat"].id,
                "next_due_date": "2026-12-01",
            },
        )
        assert created.status_code == 201
        assert created.json()["paid_from"] == "bills_fund"
        assert created.json()["paid_from_fund_id"] is None
        due = authed_client.get("/api/bills/due")
        assert due.status_code == 200
        assert due.json() == []

    def test_budget_row_pay_is_wrong_source(self, authed_client, db_session, household):
        bill = _bill(db_session, household)
        response = authed_client.post(
            f"/api/bills/{bill.id}/pay",
            json={"amount": "10", "date": "2026-11-03"},
        )
        assert response.status_code == 409
        assert response.json()["code"] == "wrong_source"
        assert "Groceries" in response.json()["detail"]
        assert "Confirm it instead" in response.json()["detail"]


class TestOccurrenceTransitions:
    """Acceptance coverage for the shared occurrence transition."""

    def _fixed(self, db, household, **kwargs):
        values = {
            "paid_from": "bills_fund",
            "category_id": household["bills_cat"].id,
            "bill_type": "fixed",
            "frequency": "monthly",
            "amount": Decimal("40.00"),
            "next_due_date": "2026-11-03",
            "name": "Power",
        }
        values.update(kwargs)
        return _bill(db, household, **values)

    def _patch_both(self, monkeypatch, day):
        monkeypatch.setattr("app.tasks._today", lambda: day)
        monkeypatch.setattr("app.services.recurring_payments._today", lambda: day)

    def test_pay_between_load_and_compare_and_set(
        self, db_session, household, monkeypatch
    ):
        day = date(2026, 11, 3)
        self._patch_both(monkeypatch, day)
        bill = self._fixed(db_session, household)
        start = Decimal(str(household["bills"].current_balance))

        def hook(db, loaded):
            pay_bills_fund(db, loaded, Decimal("40.00"), day.isoformat())
            db.commit()

        monkeypatch.setattr("app.tasks.auto_pay_before_cas", hook)
        from app.tasks import process_due_bills

        process_due_bills(db=db_session)
        db_session.rollback()
        rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].occurrence_due_date == "2026-11-03"
        db_session.refresh(household["bills"])
        assert Decimal(str(household["bills"].current_balance)) == start - Decimal(
            "40.00"
        )
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-12-03"

    def test_pay_then_auto_pay_inserts_nothing(
        self, db_session, household, monkeypatch
    ):
        day = date(2026, 11, 3)
        self._patch_both(monkeypatch, day)
        bill = self._fixed(db_session, household)
        pay_bills_fund(db_session, bill, Decimal("40.00"), day.isoformat())
        db_session.commit()
        from app.tasks import process_due_bills

        process_due_bills(db=db_session)
        db_session.rollback()
        rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .all()
        )
        assert len(rows) == 1

    def test_two_confirms_of_one_variable_date_one_payment(
        self, db_session, household, fixed_today
    ):
        bill = _bill(
            db_session,
            household,
            bill_type="variable",
            paid_from="budget",
            next_due_date="2026-10-26",
        )
        confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        db_session.commit()
        with pytest.raises(PaymentConflict) as exc:
            confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        assert exc.value.code == "stale"
        db_session.rollback()
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .count()
            == 1
        )

    def test_stale_html_edit_saves_nothing(
        self, authed_client, db_session, household, fixed_today
    ):
        bill = self._fixed(db_session, household, amount=Decimal(40))
        edit = authed_client.get(f"/bills/{bill.id}/edit")
        assert 'name="next_due_date_was"' in edit.text
        assert f'form="edit-form-{bill.id}"' in edit.text
        assert bill.next_due_date in edit.text
        paid = authed_client.post(
            f"/api/bills/{bill.id}/pay",
            json={"amount": "40.00", "date": "2026-11-03"},
        )
        assert paid.status_code == 200
        db_session.refresh(bill)
        stepped = bill.next_due_date
        original_name = "Power"
        stale = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "name": "Renamed",
                "amount": "40.00",
                "next_due_date": "2026-12-15",
                "next_due_date_was": "2026-11-03",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert stale.status_code == 409
        db_session.refresh(bill)
        assert bill.name == original_name
        assert Decimal(str(bill.amount)) == Decimal("40.00")
        assert bill.next_due_date == stepped
        missing = authed_client.post(
            f"/bills/{bill.id}",
            data={"name": "Still no", "next_due_date": "2026-01-01", "amount": "9"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert missing.status_code == 400
        db_session.refresh(bill)
        assert bill.name == original_name
        assert bill.next_due_date == stepped

    def test_delete_payment_reverses_the_pair_and_leaves_the_cursor(
        self, authed_client, db_session, household, fixed_today
    ):
        TestLateConfirm._swept_month(db_session, household, "surplus")
        bill = _bill(db_session, household, next_due_date="2026-10-26")
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        db_session.refresh(bill)
        stepped = bill.next_due_date
        claw_id = result.taken_back["transaction_id"]
        deleted = authed_client.delete(f"/api/transactions/{result.transaction.id}")
        assert deleted.status_code == 200
        db_session.refresh(household["october"])
        db_session.refresh(household["overflow"])
        assert Decimal(str(household["october"].spent_amount)) == Decimal("0.00")
        assert Decimal(str(household["overflow"].current_balance)) == Decimal("10.00")
        assert db_session.get(Transaction, result.transaction.id) is None
        assert db_session.get(Transaction, claw_id) is None
        db_session.refresh(bill)
        assert bill.next_due_date == stepped
        with pytest.raises(PaymentConflict) as stale:
            confirm_payment(db_session, bill, Decimal(10), "2026-10-26")
        assert stale.value.code == "stale"
        db_session.rollback()
        again = confirm_payment(db_session, bill, Decimal(12), stepped)
        db_session.commit()
        assert again.transaction.occurrence_due_date == stepped

    def test_delete_clawback_reverses_the_same_pair(
        self, authed_client, db_session, household, fixed_today
    ):
        TestLateConfirm._swept_month(db_session, household, "surplus")
        bill = _bill(db_session, household, next_due_date="2026-10-26")
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        db_session.refresh(bill)
        stepped = bill.next_due_date
        deleted = authed_client.delete(
            f"/api/transactions/{result.taken_back['transaction_id']}"
        )
        assert deleted.status_code == 200
        db_session.refresh(household["october"])
        db_session.refresh(household["overflow"])
        assert Decimal(str(household["october"].spent_amount)) == Decimal("0.00")
        assert Decimal(str(household["overflow"].current_balance)) == Decimal("10.00")
        assert db_session.get(Transaction, result.transaction.id) is None
        assert db_session.get(Transaction, result.taken_back["transaction_id"]) is None
        db_session.refresh(bill)
        assert bill.next_due_date == stepped

    def test_missing_partner_reverses_the_row_in_hand(
        self, authed_client, db_session, household, fixed_today
    ):
        TestLateConfirm._swept_month(db_session, household, "surplus")
        bill = _bill(db_session, household)
        result = confirm_payment(db_session, bill, Decimal(90), "2026-10-26")
        db_session.commit()
        claw_id = result.taken_back["transaction_id"]
        db_session.delete(result.transaction)
        db_session.commit()
        deleted = authed_client.delete(f"/api/transactions/{claw_id}")
        assert deleted.status_code == 200
        assert db_session.get(Transaction, claw_id) is None
        db_session.refresh(household["overflow"])
        assert Decimal(str(household["overflow"].current_balance)) == Decimal("10.00")

    def test_cursor_edited_back_onto_a_paid_date_does_not_stick(
        self, authed_client, db_session, household, fixed_today
    ):
        bill = self._fixed(db_session, household)
        pay_bills_fund(db_session, bill, Decimal(40), "2026-11-03")
        db_session.commit()
        db_session.refresh(bill)
        moved = authed_client.put(
            f"/api/bills/{bill.id}",
            json={
                "next_due_date": "2026-11-03",
                "expected_next_due_date": bill.next_due_date,
            },
        )
        assert moved.status_code == 200
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-11-03"
        again = authed_client.post(
            f"/api/bills/{bill.id}/pay",
            json={"amount": "40.00", "date": "2026-11-04"},
        )
        assert again.status_code == 409
        db_session.rollback()
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-11-03"
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .count()
            == 1
        )

    def test_manual_pay_past_end_date_is_ended(
        self, db_session, household, fixed_today
    ):
        bill = self._fixed(db_session, household, end_date="2026-11-01")
        with pytest.raises(PaymentConflict) as exc:
            pay_bills_fund(db_session, bill, Decimal(40), "2026-11-03")
        assert exc.value.code == "ended"
        db_session.rollback()
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-11-03"
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .count()
            == 0
        )

    def test_pay_on_end_date_can_step_past_the_end(
        self, db_session, household, fixed_today
    ):
        bill = self._fixed(db_session, household, end_date="2026-11-03")
        txn = pay_bills_fund(db_session, bill, Decimal(40), "2026-11-03")
        db_session.commit()
        assert txn.occurrence_due_date == "2026-11-03"
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-12-03"

    def test_auto_confirm_and_skip_after_end_write_nothing(
        self, db_session, household, monkeypatch
    ):
        day = date(2026, 11, 3)
        self._patch_both(monkeypatch, day)
        auto = self._fixed(
            db_session, household, name="Ended auto", end_date="2026-11-01"
        )
        variable = _bill(
            db_session,
            household,
            name="Ended variable",
            bill_type="variable",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            next_due_date="2026-11-03",
            end_date="2026-11-01",
        )
        budget = _bill(
            db_session,
            household,
            name="Ended budget",
            next_due_date="2026-10-26",
            end_date="2026-10-01",
        )
        from app.tasks import process_due_bills

        process_due_bills(db=db_session)
        with pytest.raises(PaymentConflict) as confirmed:
            confirm_payment(db_session, budget, Decimal(10), "2026-10-26")
        assert confirmed.value.code == "ended"
        with pytest.raises(PaymentConflict) as skipped:
            skip_payment(db_session, variable, "2026-11-03")
        assert skipped.value.code == "ended"
        db_session.rollback()
        for row in (auto, variable, budget):
            db_session.refresh(row)
        assert auto.next_due_date == "2026-11-03"
        assert variable.next_due_date == "2026-11-03"
        assert budget.next_due_date == "2026-10-26"
        assert db_session.query(Transaction).count() == 0

    def test_json_and_mcp_require_the_date_pair(
        self, authed_client, db_session, household
    ):
        from app.mcp_server import update_bill

        bill = self._fixed(db_session, household)
        before = (bill.name, bill.amount, bill.next_due_date)
        refused = authed_client.put(
            f"/api/bills/{bill.id}",
            json={"next_due_date": "2026-12-01", "name": "Nope"},
        )
        assert refused.status_code == 400
        db_session.refresh(bill)
        assert (bill.name, bill.amount, bill.next_due_date) == before
        mcp = update_bill(bill.id, next_due_date="2026-12-01", name="Nope")
        assert isinstance(mcp, str)
        db_session.refresh(bill)
        assert (bill.name, bill.amount, bill.next_due_date) == before
        saved = authed_client.put(
            f"/api/bills/{bill.id}",
            json={"name": "Kept cursor"},
        )
        assert saved.status_code == 200
        db_session.refresh(bill)
        assert bill.name == "Kept cursor"
        assert bill.next_due_date == before[2]
        renamed = update_bill(bill.id, name="From MCP")
        assert isinstance(renamed, dict)
        assert renamed["name"] == "From MCP"
        db_session.refresh(bill)
        assert bill.next_due_date == before[2]

    def test_html_rejects_an_impossible_date(
        self, authed_client, db_session, household
    ):
        created = authed_client.post(
            "/bills",
            data={
                "name": "Bad day",
                "debtor_provider": "X",
                "amount": "10",
                "frequency": "monthly",
                "start_date": "2026-02-31",
                "next_due_date": "2026-03-01",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert created.status_code == 400
        assert (
            db_session.query(RecurringBill)
            .filter(RecurringBill.name == "Bad day")
            .count()
            == 0
        )
        bill = self._fixed(db_session, household)
        edited = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "name": "Also bad",
                "next_due_date": "2026-02-31",
                "next_due_date_was": bill.next_due_date,
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert edited.status_code == 400
        db_session.refresh(bill)
        assert bill.name == "Power"
        assert bill.next_due_date == "2026-11-03"

    def test_same_due_date_save_succeeds(self, authed_client, db_session, household):
        bill = self._fixed(db_session, household)
        saved = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "name": "Same day",
                "next_due_date": "2026-11-03",
                "next_due_date_was": "2026-11-03",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert saved.status_code == 200
        db_session.refresh(bill)
        assert bill.name == "Same day"
        assert bill.next_due_date == "2026-11-03"


class TestLateConfirm:
    @staticmethod
    def _swept_month(db, household, outcome, *, fund=None, deleted=False):
        target = household["overflow"] if fund is None else fund
        if deleted:
            target.is_deleted = True
        _set_overflow(db, household)
        _income_in(db, household, "2026-11-01")
        if outcome == "missing":
            return
        row = HouseholdMonth(
            year=2026,
            month=10,
            settlement_outcome=outcome,
            settlement_fund_id=None
            if outcome in ("no_overflow", "unresolved")
            else target.id,
            settlement_amount=0 if outcome == "zero" else 20,
            settled_on="2026-11-01",
        )
        db.add(row)
        db.commit()

    def _confirm(self, db, household, amount="15.00"):
        from app.models import Contract

        contract = Contract(
            name="Box",
            provider="HelloFresh",
            service="other",
            bill_delivery="email",
            ended_on="2026-01-01",
        )
        db.add(contract)
        db.commit()
        bill = _bill(
            db,
            household,
            next_due_date="2026-10-26",
            contract_id=contract.id,
            name="HelloFresh",
        )
        # Today's overflow setting is a different fund from the stored one.
        alloc = db.query(IncomeAllocation).one()
        alloc.overflow_sinking_fund_id = household["kids"].id
        db.commit()
        result = confirm_payment(db, bill, Decimal(amount), "2026-10-26")
        db.commit()
        return bill, result, contract

    @pytest.mark.parametrize("outcome", ["surplus", "shortfall", "zero"])
    def test_live_stored_fund_is_debited(
        self, db_session, household, fixed_today, outcome
    ):
        self._swept_month(db_session, household, outcome)
        before_kids = Decimal(str(household["kids"].current_balance))
        _bill_row, result, contract = self._confirm(db_session, household)
        txn = result.transaction
        assert txn.date == "2026-10-26"
        assert txn.contract_id == contract.id
        assert txn.occurrence_due_date == "2026-10-26"
        assert result.taken_back is not None
        claw = db_session.get(Transaction, result.taken_back["transaction_id"])
        assert claw.date == "2026-11-03"
        assert Decimal(str(claw.amount)) == Decimal("15.00")
        assert claw.sinking_fund_id == household["overflow"].id
        assert claw.contract_id is None
        assert claw.occurrence_due_date is None
        assert claw.adjustment_for_transaction_id == txn.id
        assert claw.description == "Taken back for HelloFresh (October 2026 Groceries)"
        db_session.refresh(household["kids"])
        assert Decimal(str(household["kids"].current_balance)) == before_kids

    @pytest.mark.parametrize(
        "outcome", ["no_overflow", "unresolved", "missing", "deleted"]
    )
    def test_those_months_do_not_debit(
        self, db_session, household, fixed_today, outcome
    ):
        if outcome == "deleted":
            self._swept_month(db_session, household, "surplus", deleted=True)
        else:
            self._swept_month(db_session, household, outcome)
        _bill_row, result, _contract = self._confirm(db_session, household)
        assert result.transaction.occurrence_due_date == "2026-10-26"
        assert result.taken_back is None
        assert (
            db_session.query(Transaction)
            .filter(Transaction.description.like("Taken back%"))
            .count()
            == 0
        )

    def test_bills_fund_auto_pay_does_not_claw_back(
        self, db_session, household, monkeypatch
    ):
        day = date(2026, 11, 3)
        monkeypatch.setattr("app.tasks._today", lambda: day)
        monkeypatch.setattr("app.services.recurring_payments._today", lambda: day)
        self._swept_month(db_session, household, "surplus")
        # The swept month is October. Point a fixed bill at that month too.
        bill = _bill(
            db_session,
            household,
            name="Rates",
            paid_from="bills_fund",
            category_id=household["bills_cat"].id,
            bill_type="fixed",
            frequency="monthly",
            amount=Decimal(25),
            next_due_date="2026-10-01",
        )
        before = Decimal(str(household["overflow"].current_balance))
        from app.tasks import process_due_bills

        process_due_bills(db=db_session)
        db_session.rollback()
        rows = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .all()
        )
        assert len(rows) == 1
        assert rows[0].description.startswith("Auto-payment:")
        assert (
            db_session.query(Transaction)
            .filter(Transaction.description.like("Taken back%"))
            .count()
            == 0
        )
        db_session.refresh(household["overflow"])
        assert Decimal(str(household["overflow"].current_balance)) == before
        assert household["overflow"].name != "Bills"
