"""Every 2 years bill frequency: dates, cost, and unknown-frequency failures."""

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from app.cli.main import app as cli_app
from app.mcp_server import create_bill, skip_payment, update_bill
from app.models import (
    Category,
    IncomeAllocation,
    RecurringBill,
    SinkingFund,
    Transaction,
)
from app.routes.bills import _compute_annual_cost
from app.schemas import (
    FREQUENCY_ANNUAL_MULTIPLIER,
    FREQUENCY_LABELS,
    BillFrequency,
    UnknownFrequencyError,
    annual_multiplier,
)
from app.services.bills_cover import compute_bills_cover
from app.services.household_months import is_processed
from app.services.recurring_payments import pay_bills_fund
from app.services.recurring_payments import skip_payment as skip_bill
from app.tasks import (
    _compute_bills_recommended,
    advance_due_date,
    generate_bills_forecast,
    iter_bill_due_dates,
    process_due_bills,
    process_income_allocation,
)

_RUNNER = CliRunner()


def test_frequency_tables_share_keys():
    values = {item.value for item in BillFrequency}
    assert values == set(FREQUENCY_LABELS) == set(FREQUENCY_ANNUAL_MULTIPLIER)
    assert FREQUENCY_LABELS["every_2_years"] == "Every 2 Years"
    assert FREQUENCY_ANNUAL_MULTIPLIER["every_2_years"] == Decimal("0.5")
    assert annual_multiplier("every_2_years") == Decimal("0.5")
    length = RecurringBill.__table__.c.frequency.type.length
    assert length is not None
    assert length >= len("every_2_years")


class TestAdvanceEveryTwoYears:
    def test_31_jan_stays_on_the_31st(self):
        assert advance_due_date(date(2026, 1, 31), "every_2_years") == date(2028, 1, 31)

        walked = date(2026, 1, 31)
        for _ in range(24):
            walked = advance_due_date(walked, "monthly")
        assert walked != date(2028, 1, 31)

    def test_29_feb_clamps_then_stays_on_the_28th(self):
        first = advance_due_date(date(2028, 2, 29), "every_2_years")
        assert first == date(2030, 2, 28)
        assert advance_due_date(first, "every_2_years") == date(2032, 2, 28)
        assert list(
            iter_bill_due_dates("2028-02-29", "every_2_years", date(2032, 2, 28))
        ) == [
            date(2028, 2, 29),
            date(2030, 2, 28),
            date(2032, 2, 28),
        ]


def _bill(db, category_id, **overrides) -> RecurringBill:
    fields = {
        "name": "Rates",
        "amount": 240,
        "debtor_provider": "Council",
        "start_date": "2026-01-01",
        "frequency": "every_2_years",
        "category_id": category_id,
        "next_due_date": "2026-06-15",
        "paid_from": "bills_fund",
        "bill_type": "fixed",
    }
    fields.update(overrides)
    bill = RecurringBill(**fields)
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return bill


def _bills_fund(db, balance=0) -> SinkingFund:
    fund = SinkingFund(name="Bills", color="#FF0000", current_balance=balance)
    db.add(fund)
    db.commit()
    db.refresh(fund)
    return fund


class TestCost:
    def test_annual_and_monthly_are_amount_over_24(self, db_session, sample_category):
        _bill(db_session, sample_category.id, amount=240)
        assert _compute_annual_cost(240, "every_2_years") == Decimal(120)
        assert _compute_bills_recommended(db_session) == Decimal("10.00")

    def test_bills_page_shows_amount_over_24(
        self, authed_client, db_session, sample_category
    ):
        created = authed_client.post(
            "/bills",
            data={
                "name": "Rates",
                "debtor_provider": "Council",
                "amount": "240.00",
                "frequency": "every_2_years",
                "start_date": "2026-01-01",
                "next_due_date": "2026-06-15",
                "paid_from": "bills_fund",
                "bill_type": "fixed",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert created.status_code == 200
        assert "Every 2 Years" in created.text
        trigger = created.headers["hx-trigger-after-swap"]
        assert '"monthly": "10.00"' in trigger
        assert '"annual": "120.00"' in trigger
        page = authed_client.get("/bills")
        assert 'id="bills-monthly-cost">$10.00' in page.text
        assert 'id="bills-annual-cost">$120.00' in page.text
        stored = db_session.query(RecurringBill).filter_by(name="Rates").one()
        assert stored.frequency == "every_2_years"


class TestForecastAndCover:
    @patch("app.tasks._today")
    def test_forecast_includes_the_full_amount_in_the_due_month(
        self, mock_today, db_session, sample_category
    ):
        mock_today.return_value = date(2026, 3, 18)
        _bills_fund(db_session, balance=1000)
        allocation = IncomeAllocation(
            monthly_income_amount=5000,
            monthly_budget_allocation=800,
            bills_fund_allocation_type="recommended",
        )
        db_session.add(allocation)
        db_session.commit()
        _bill(db_session, sample_category.id, next_due_date="2026-06-15", amount=240)

        rows = generate_bills_forecast(db_session)
        due = [row for row in rows if row["bills"]]
        assert len(due) == 1
        assert (due[0]["year"], due[0]["month"]) == (2026, 6)
        assert due[0]["bills"][0]["amount"] == Decimal(240)
        assert due[0]["total_out"] == Decimal("240.00")
        assert all(row["configured_contribution"] == Decimal("10.00") for row in rows)
        assert all(
            row["total_out"] == Decimal("0.00")
            for row in rows
            if (row["year"], row["month"]) != (2026, 6)
        )

    def test_cover_includes_the_full_amount_inside_the_horizon(
        self, db_session, sample_category, sample_sinking_funds
    ):
        today = date(2026, 3, 18)
        _bill(db_session, sample_category.id, next_due_date="2026-06-15", amount=240)
        inside = compute_bills_cover(db_session, today=today, horizon_months=3)
        wider = compute_bills_cover(db_session, today=today, horizon_months=12)
        assert inside.obligations == Decimal("240.00")
        assert inside.projected_count == 1
        assert wider.obligations == Decimal("240.00")
        assert wider.projected_count == 1

        outside = _bill(
            db_session,
            sample_category.id,
            name="Later",
            next_due_date="2026-08-01",
            amount=240,
        )
        short = compute_bills_cover(db_session, today=today, horizon_months=3)
        long = compute_bills_cover(db_session, today=today, horizon_months=12)
        assert short.obligations == Decimal("240.00")
        assert long.obligations == Decimal("480.00")
        assert outside.next_due_date == "2026-08-01"


class TestAcceptance:
    def test_add_form_lists_every_2_years(self, authed_client):
        page = authed_client.get("/bills")
        assert 'value="every_2_years"' in page.text
        assert "Every 2 Years" in page.text

    def test_edit_form_selects_every_2_years(
        self, authed_client, db_session, sample_category
    ):
        bill = _bill(db_session, sample_category.id)
        form = authed_client.get(f"/bills/{bill.id}/edit")
        assert form.status_code == 200
        assert 'value="every_2_years" selected' in form.text
        saved = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "name": "Rates",
                "debtor_provider": "Council",
                "amount": "240.00",
                "frequency": "every_2_years",
                "next_due_date": "2026-06-15",
                "next_due_date_was": "2026-06-15",
                "paid_from": "bills_fund",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert saved.status_code == 200
        db_session.refresh(bill)
        assert bill.frequency == "every_2_years"

    def test_api_accepts_every_2_years(
        self, authed_client, db_session, sample_category
    ):
        created = authed_client.post(
            "/api/bills",
            json={
                "name": "Rates",
                "amount": "240.00",
                "debtor_provider": "Council",
                "start_date": "2026-01-01",
                "frequency": "every_2_years",
                "category_id": sample_category.id,
                "next_due_date": "2026-06-15",
            },
        )
        assert created.status_code == 201
        assert created.json()["frequency"] == "every_2_years"
        updated = authed_client.put(
            f"/api/bills/{created.json()['id']}",
            json={"frequency": "yearly"},
        )
        assert updated.status_code == 200
        assert updated.json()["frequency"] == "yearly"
        restored = authed_client.put(
            f"/api/bills/{created.json()['id']}",
            json={"frequency": "every_2_years"},
        )
        assert restored.status_code == 200
        assert restored.json()["frequency"] == "every_2_years"

    def test_mcp_accepts_every_2_years(self, db_session, sample_category):
        assert "every_2_years" in (create_bill.__doc__ or "")
        assert "every_2_years" in (update_bill.__doc__ or "")
        created = create_bill(
            name="Rates",
            amount=240,
            debtor_provider="Council",
            start_date="2026-01-01",
            frequency="every_2_years",
            category_id=sample_category.id,
            next_due_date="2026-06-15",
        )
        assert isinstance(created, dict)
        assert created["frequency"] == "every_2_years"
        updated = update_bill(created["id"], frequency="every_2_years")
        assert isinstance(updated, dict)
        assert updated["frequency"] == "every_2_years"

    def test_cli_help_lists_every_2_years(self):
        result = _RUNNER.invoke(cli_app, ["bills", "add", "--help"])
        assert result.exit_code == 0
        assert "every_2_years" in result.stdout


class TestDueDateCallers:
    def test_skip_steps_24_months_including_the_leap_day(
        self, db_session, sample_category
    ):
        bill = _bill(
            db_session,
            sample_category.id,
            bill_type="variable",
            next_due_date="2028-02-29",
        )
        skip_bill(db_session, bill, "2028-02-29")
        db_session.commit()
        db_session.refresh(bill)
        assert bill.next_due_date == "2030-02-28"
        skip_bill(db_session, bill, "2030-02-28")
        db_session.commit()
        db_session.refresh(bill)
        assert bill.next_due_date == "2032-02-28"
        assert db_session.query(Transaction).count() == 0

    def test_pay_and_autopay_step_24_months(self, db_session, sample_category):
        fund = _bills_fund(db_session, balance=1000)
        paid = _bill(
            db_session,
            sample_category.id,
            name="Paid",
            next_due_date="2026-01-31",
            amount=240,
        )
        txn = pay_bills_fund(db_session, paid, Decimal(240), "2026-01-31").transaction
        db_session.commit()
        db_session.refresh(paid)
        db_session.refresh(fund)
        assert paid.next_due_date == "2028-01-31"
        assert Decimal(str(txn.amount)) == Decimal("240.00")
        assert Decimal(str(fund.current_balance)) == Decimal("760.00")

        auto = _bill(
            db_session,
            sample_category.id,
            name="Auto",
            next_due_date="2026-01-31",
            amount=100,
        )
        with patch("app.tasks._today", return_value=date(2026, 1, 31)):
            process_due_bills(db=db_session)
        db_session.refresh(auto)
        assert auto.next_due_date == "2028-01-31"
        row = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == auto.id)
            .one()
        )
        assert Decimal(str(row.amount)) == Decimal(100)


def _payday(db, *, frequency: str):
    income = Category(name="Salary", type="income", color="#00FF00")
    expense = Category(name="Bills", type="expense", color="#FF0000")
    transfer = Category(name="Transfer", type="transfer", color="#6B7280")
    db.add_all([income, expense, transfer])
    db.flush()
    fund = SinkingFund(name="Bills", color="#FF0000", current_balance=500)
    db.add(fund)
    db.flush()
    bill = RecurringBill(
        name="Rates",
        amount=240,
        debtor_provider="Council",
        start_date="2026-01-01",
        frequency=frequency,
        category_id=expense.id,
        next_due_date="2026-06-15",
    )
    allocation = IncomeAllocation(
        monthly_income_amount=5000,
        monthly_budget_allocation=800,
        bills_fund_allocation_type="recommended",
    )
    db.add_all([bill, allocation])
    db.commit()
    return fund


class TestUnknownFrequency:
    def test_advance_and_multipliers_raise(self, db_session, sample_category):
        assert not issubclass(UnknownFrequencyError, ValueError)
        with pytest.raises(UnknownFrequencyError) as raised:
            advance_due_date(date(2026, 1, 31), "nope")
        assert raised.value.frequency == "nope"
        assert not isinstance(raised.value, ValueError)
        with pytest.raises(UnknownFrequencyError):
            annual_multiplier("nope")
        with pytest.raises(UnknownFrequencyError):
            _compute_annual_cost(240, "nope")
        _bill(db_session, sample_category.id, frequency="nope")
        with pytest.raises(UnknownFrequencyError):
            _compute_bills_recommended(db_session)

    def test_edit_form_does_not_fall_back_to_weekly(
        self, authed_client, db_session, sample_category
    ):
        bill = _bill(db_session, sample_category.id, frequency="whenever")
        form = authed_client.get(f"/bills/{bill.id}/edit")
        assert form.status_code == 200
        assert 'value="whenever" selected' in form.text
        assert 'value="weekly" selected' not in form.text
        saved = authed_client.post(
            f"/bills/{bill.id}",
            data={
                "frequency": "whenever",
                "amount": "240.00",
                "next_due_date": bill.next_due_date,
                "next_due_date_was": bill.next_due_date,
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert saved.status_code == 400
        assert "Choose a frequency." in saved.text
        db_session.refresh(bill)
        assert bill.frequency == "whenever"

    def test_confirm_and_skip_are_not_a_bad_date(
        self, authed_client, db_session, sample_category
    ):
        _bills_fund(db_session, balance=500)
        bill = _bill(
            db_session,
            sample_category.id,
            frequency="nope",
            bill_type="variable",
            next_due_date="2026-01-31",
        )
        skipped = authed_client.post(
            f"/bills/{bill.id}/skip",
            data={"due_date": "2026-01-31"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert skipped.status_code == 400
        assert "Unknown bill frequency: nope" in skipped.text
        assert "Invalid due date" not in skipped.text
        confirmed = authed_client.post(
            f"/bills/{bill.id}/confirm",
            data={"amount": "240.00", "due_date": "2026-01-31"},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert confirmed.status_code == 400
        assert "Unknown bill frequency: nope" in confirmed.text
        assert "Invalid due date" not in confirmed.text
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-01-31"
        assert db_session.query(Transaction).count() == 0
        mcp = skip_payment(bill.id, "2026-01-31")
        assert isinstance(mcp, str)
        assert "Validation error" not in mcp
        assert "Unknown bill frequency: nope" in mcp
        paid = authed_client.post(
            f"/api/bills/{bill.id}/pay",
            json={"amount": "240.00", "date": "2026-01-31"},
        )
        assert paid.status_code == 400
        assert paid.json()["code"] == "unknown_frequency"
        assert "Invalid due date" not in paid.json()["detail"]
        db_session.refresh(bill)
        assert bill.next_due_date == "2026-01-31"
        assert db_session.query(Transaction).count() == 0

    def test_cover_does_not_skip_it_as_a_bad_date(
        self, db_session, sample_category, sample_sinking_funds
    ):
        _bill(
            db_session,
            sample_category.id,
            frequency="nope",
            next_due_date="2026-03-20",
        )
        with pytest.raises(UnknownFrequencyError):
            compute_bills_cover(db_session, today=date(2026, 3, 10), horizon_months=3)

    @patch("app.tasks._today")
    def test_forecast_page_shows_the_error(
        self, mock_today, authed_client, db_session, sample_category
    ):
        mock_today.return_value = date(2026, 3, 18)
        fund = _bills_fund(db_session)
        _bill(
            db_session,
            sample_category.id,
            frequency="nope",
            next_due_date="2026-06-15",
        )
        with pytest.raises(UnknownFrequencyError):
            generate_bills_forecast(db_session)
        page = authed_client.get(f"/sinking-funds/{fund.id}/forecast")
        assert page.status_code == 200
        assert "does not know (nope)" in page.text
        assert "Internal Server Error" not in page.text

    @patch("app.tasks._today")
    def test_payday_rolls_the_month_back(self, mock_today, db_session):
        mock_today.return_value = date(2026, 2, 1)
        fund = _payday(db_session, frequency="nope")
        with pytest.raises(UnknownFrequencyError):
            process_income_allocation(db=db_session)
        assert db_session.query(Transaction).count() == 0
        db_session.refresh(fund)
        assert Decimal(str(fund.current_balance)) == Decimal(500)
        assert is_processed(db_session, 2026, 2) is False

    @patch("app.tasks._today")
    def test_payday_posts_amount_over_24(self, mock_today, db_session):
        mock_today.return_value = date(2026, 2, 1)
        fund = _payday(db_session, frequency="every_2_years")
        process_income_allocation(db=db_session)
        deposit = (
            db_session.query(Transaction)
            .filter(
                Transaction.sinking_fund_id == fund.id,
                Transaction.transaction_type == "income_allocation",
            )
            .one()
        )
        assert Decimal(str(deposit.amount)) == Decimal("10.00")
        db_session.refresh(fund)
        assert Decimal(str(fund.current_balance)) == Decimal("510.00")

    @patch("app.tasks._today")
    def test_autopay_fails_only_the_unknown_bill(
        self, mock_today, db_session, sample_category
    ):
        mock_today.return_value = date(2026, 2, 1)
        fund = _bills_fund(db_session, balance=5000)
        good = _bill(
            db_session,
            sample_category.id,
            name="Rent",
            frequency="monthly",
            amount=2400,
            next_due_date="2026-02-01",
        )
        bad = _bill(
            db_session,
            sample_category.id,
            name="Mystery",
            frequency="nope",
            amount=80,
            next_due_date="2026-02-01",
        )
        process_due_bills(db=db_session)
        db_session.rollback()
        db_session.refresh(fund)
        db_session.refresh(good)
        db_session.refresh(bad)
        assert Decimal(str(fund.current_balance)) == Decimal(2600)
        assert good.next_due_date == "2026-03-01"
        assert bad.next_due_date == "2026-02-01"
        paid = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == good.id)
            .one()
        )
        assert paid.occurrence_due_date == "2026-02-01"
        assert (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bad.id)
            .count()
            == 0
        )
