from datetime import datetime

from app.config import TIMEZONE as BRISBANE
from app.models import (
    Budget,
    Category,
    IncomeAllocation,
    MonthlyUnallocatedIncome,
    SinkingFund,
    Transaction,
)


def _current_month_year():
    now = datetime.now(BRISBANE)
    return now.month, now.year


class TestDashboardPageGet:
    def test_renders_page(self, authed_client):
        response = authed_client.get("/")
        assert response.status_code == 200
        assert "Dashboard" in response.text or "Total Income" in response.text

    def test_defaults_to_current_month(self, authed_client):
        month, year = _current_month_year()
        import calendar

        month_name = calendar.month_name[month]
        response = authed_client.get("/")
        assert response.status_code == 200
        assert month_name in response.text
        assert str(year) in response.text

    def test_financial_summary_with_data(self, authed_client, sample_transactions):
        response = authed_client.get("/?month=1&year=2026")
        assert response.status_code == 200
        assert "5,000.00" in response.text  # income
        assert "75.50" in response.text  # expense
        assert "4,924.50" in response.text  # net

    def test_budget_overview(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.get(f"/?month={month}&year={year}")
        assert response.status_code == 200
        assert "Budget Overview" in response.text
        assert "800.00" in response.text  # allocated: 600 + 200
        assert "230.00" in response.text  # spent: 150 + 80
        assert "570.00" in response.text  # remaining: 800 - 230

    def test_sinking_funds_list(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/")
        assert response.status_code == 200
        assert "Bills" in response.text
        assert "Savings" in response.text

    def test_recent_transactions(self, authed_client, sample_transactions):
        response = authed_client.get("/?month=1&year=2026")
        assert response.status_code == 200
        assert "Groceries shopping" in response.text
        assert "Monthly income" in response.text

    def test_empty_month_shows_zeros(self, authed_client):
        response = authed_client.get("/?month=6&year=2030")
        assert response.status_code == 200
        assert "0.00" in response.text
        assert "No transactions this month." in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_month_year_params(self, authed_client, sample_transactions):
        response = authed_client.get("/?month=1&year=2026")
        assert response.status_code == 200
        assert "January" in response.text
        assert "2026" in response.text

    def test_unallocated_income_displayed(self, authed_client, db_session):
        month, year = _current_month_year()
        row = MonthlyUnallocatedIncome(
            month=month,
            year=year,
            unallocated_amount=350.75,
        )
        db_session.add(row)
        db_session.commit()

        response = authed_client.get(f"/?month={month}&year={year}")
        assert response.status_code == 200
        assert "350.75" in response.text

    def test_transaction_limit_10(self, authed_client, db_session):
        cat = Category(
            name="Test", type="expense", color="#123456", is_budget_category=False
        )
        db_session.add(cat)
        db_session.commit()
        db_session.refresh(cat)

        for i in range(15):
            txn = Transaction(
                date=f"2026-03-{(i % 28) + 1:02d}",
                description=f"txn-{i}",
                amount=10.00,
                category_id=cat.id,
                type="expense",
                transaction_type="regular",
            )
            db_session.add(txn)
        db_session.commit()

        response = authed_client.get("/?month=3&year=2026")
        assert response.status_code == 200
        # Count transaction rows in the table body
        # At most 10 rows should appear
        count = response.text.count("txn-")
        assert count == 10


class TestQuickExpenseForm:
    def test_form_renders_when_budgets_exist(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.get(f"/?month={month}&year={year}")
        assert response.status_code == 200
        assert "Quick Expense" in response.text
        assert 'hx-post="/dashboard/quick-expense"' in response.text
        assert "Groceries" in response.text
        assert "Transport" in response.text

    def test_form_hidden_when_no_budgets(self, authed_client):
        response = authed_client.get("/?month=6&year=2030")
        assert response.status_code == 200
        assert 'hx-post="/dashboard/quick-expense"' not in response.text

    def test_successful_submission(self, authed_client, db_session, sample_budgets):
        budget = sample_budgets[0]  # Groceries, spent=150, allocated=600
        month, year = _current_month_year()
        response = authed_client.post(
            "/dashboard/quick-expense",
            data={
                "budget_id": str(budget.id),
                "amount": "42.50",
                "date": f"{year}-{month:02d}-15",
                "month": str(month),
                "year": str(year),
            },
            headers={"x-csrftoken": authed_client.csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 200
        assert "HX-Redirect" in response.headers

        # Verify transaction was created
        txn = (
            db_session.query(Transaction)
            .filter(
                Transaction.budget_id == budget.id,
                Transaction.transaction_type == "budget_expense",
            )
            .first()
        )
        assert txn is not None
        assert float(txn.amount) == 42.50
        assert txn.type == "expense"
        assert txn.category_id == budget.category_id

        # Verify budget spent_amount was incremented
        db_session.refresh(budget)
        assert float(budget.spent_amount) == 192.50  # 150 + 42.50

    def test_missing_budget_id(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.post(
            "/dashboard/quick-expense",
            data={
                "budget_id": "",
                "amount": "10.00",
                "date": f"{year}-{month:02d}-15",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Budget is required" in response.text

    def test_invalid_amount_zero(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.post(
            "/dashboard/quick-expense",
            data={
                "budget_id": str(sample_budgets[0].id),
                "amount": "0",
                "date": f"{year}-{month:02d}-15",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "greater than zero" in response.text

    def test_invalid_amount_text(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.post(
            "/dashboard/quick-expense",
            data={
                "budget_id": str(sample_budgets[0].id),
                "amount": "abc",
                "date": f"{year}-{month:02d}-15",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Invalid amount" in response.text

    def test_missing_date(self, authed_client, sample_budgets):
        response = authed_client.post(
            "/dashboard/quick-expense",
            data={
                "budget_id": str(sample_budgets[0].id),
                "amount": "10.00",
                "date": "",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "valid date" in response.text

    def test_budget_not_found(self, authed_client):
        response = authed_client.post(
            "/dashboard/quick-expense",
            data={
                "budget_id": "99999",
                "amount": "10.00",
                "date": "2026-01-15",
            },
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert response.status_code == 200
        assert "Budget not found" in response.text

    def test_unauthenticated_blocked(self, client):
        response = client.post(
            "/dashboard/quick-expense",
            data={
                "budget_id": "1",
                "amount": "10.00",
                "date": "2026-01-15",
            },
            follow_redirects=False,
        )
        # CSRF middleware rejects before auth can redirect
        assert response.status_code in (303, 403)


class TestApiDashboard:
    def test_json_structure(self, authed_client):
        response = authed_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()
        assert "total_income" in data
        assert "total_expenses" in data
        assert "net" in data
        assert "unallocated_income" in data
        assert "budget_total_allocated" in data
        assert "budget_total_spent" in data
        assert "budget_total_remaining" in data
        assert "sinking_funds" in data
        assert "recent_transactions" in data

    def test_defaults_to_current_month(self, authed_client):
        response = authed_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()
        # With no data, totals should be zero-like strings
        assert data["total_income"] == "0.00"

    def test_income_expense_totals(self, authed_client, sample_transactions):
        response = authed_client.get("/api/dashboard?month=1&year=2026")
        assert response.status_code == 200
        data = response.json()
        assert data["total_income"] == "5000.00"
        assert data["total_expenses"] == "75.50"
        assert data["net"] == "4924.50"

    def test_budget_totals(self, authed_client, sample_budgets):
        month, year = _current_month_year()
        response = authed_client.get(f"/api/dashboard?month={month}&year={year}")
        assert response.status_code == 200
        data = response.json()
        assert data["budget_total_allocated"] == "800.00"
        assert data["budget_total_spent"] == "230.00"
        assert data["budget_total_remaining"] == "570.00"

    def test_sinking_funds_in_response(self, authed_client, sample_sinking_funds):
        response = authed_client.get("/api/dashboard")
        assert response.status_code == 200
        data = response.json()
        assert len(data["sinking_funds"]) == 2
        names = [sf["name"] for sf in data["sinking_funds"]]
        assert "Bills" in names
        assert "Savings" in names

    def test_unallocated_income_default(self, authed_client):
        response = authed_client.get("/api/dashboard?month=6&year=2030")
        assert response.status_code == 200
        data = response.json()
        assert data["unallocated_income"] == "0.00"

    def test_unallocated_income_from_db(self, authed_client, db_session):
        month, year = _current_month_year()
        row = MonthlyUnallocatedIncome(
            month=month,
            year=year,
            unallocated_amount=123.45,
        )
        db_session.add(row)
        db_session.commit()

        response = authed_client.get(f"/api/dashboard?month={month}&year={year}")
        assert response.status_code == 200
        data = response.json()
        assert data["unallocated_income"] == "123.45"

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/api/dashboard", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_empty_month(self, authed_client):
        response = authed_client.get("/api/dashboard?month=6&year=2030")
        assert response.status_code == 200
        data = response.json()
        assert data["total_income"] == "0.00"
        assert data["total_expenses"] == "0.00"
        assert data["net"] == "0.00"
        assert data["recent_transactions"] == []


class TestBudgetOverdraftWarning:
    def _budget(self, db_session, sample_budget_categories):
        from datetime import datetime

        from app.config import TIMEZONE

        now = datetime.now(TIMEZONE)
        b = Budget(
            category_id=sample_budget_categories[0].id,
            month=now.month,
            year=now.year,
            allocated_amount=500,
            spent_amount=150,
            fund_balance=0,
        )
        db_session.add(b)
        db_session.commit()
        db_session.refresh(b)
        return b

    def test_no_warning_within_budget(
        self, authed_client, db_session, sample_budget_categories
    ):
        b = self._budget(db_session, sample_budget_categories)
        resp = authed_client.get(
            f"/dashboard/budget-overdraft-warning?budget_id={b.id}&amount=300"
        )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_warning_with_overflow_fund(
        self, authed_client, db_session, sample_budget_categories
    ):
        b = self._budget(db_session, sample_budget_categories)
        fund = SinkingFund(
            name="Short Term Savings", color="#AABBCC", current_balance=1000
        )
        db_session.add(fund)
        db_session.flush()
        alloc = IncomeAllocation(
            monthly_income_amount=5000,
            monthly_budget_allocation=1000,
            overflow_sinking_fund_id=fund.id,
        )
        db_session.add(alloc)
        db_session.commit()

        # remaining = 500 - 150 = 350; amount=400 → overdraft=$50
        resp = authed_client.get(
            f"/dashboard/budget-overdraft-warning?budget_id={b.id}&amount=400"
        )
        assert resp.status_code == 200
        assert "50" in resp.text
        assert "Short Term Savings" in resp.text
        assert "end of month" in resp.text

    def test_warning_no_overflow_fund_configured(
        self, authed_client, db_session, sample_budget_categories
    ):
        b = self._budget(db_session, sample_budget_categories)
        resp = authed_client.get(
            f"/dashboard/budget-overdraft-warning?budget_id={b.id}&amount=400"
        )
        assert resp.status_code == 200
        assert "50" in resp.text
        assert "No overflow fund" in resp.text

    def test_no_warning_for_budget_transfer_type(
        self, authed_client, db_session, sample_budget_categories
    ):
        b = self._budget(db_session, sample_budget_categories)
        resp = authed_client.get(
            f"/dashboard/budget-overdraft-warning?budget_id={b.id}&amount=400&transaction_type=budget_transfer"
        )
        assert resp.status_code == 200
        assert resp.text == ""

    def test_no_warning_missing_params(self, authed_client):
        resp = authed_client.get("/dashboard/budget-overdraft-warning")
        assert resp.status_code == 200
        assert resp.text == ""


class TestPaymentMethodBanner:
    def test_banner_absent_without_a_problem(self, authed_client):
        response = authed_client.get("/")
        assert 'id="payment-methods-banner"' not in response.text

    def test_expired_card_shows_on_any_month(
        self, authed_client, db_session, sample_category
    ):
        from app.models import PaymentMethod, RecurringBill

        method = PaymentMethod(
            nickname="Everyday Visa",
            kind="card",
            last4="4321",
            expiry_month=10,
            expiry_year=2026,
            owner="Jordan",
        )
        db_session.add(method)
        db_session.flush()
        db_session.add(
            RecurringBill(
                name="Netflix",
                amount=15,
                debtor_provider="Netflix",
                start_date="2026-01-01",
                frequency="monthly",
                category_id=sample_category.id,
                next_due_date="2026-11-05",
                payment_method_id=method.id,
            )
        )
        db_session.commit()
        current = authed_client.get("/")
        assert 'id="payment-methods-banner"' in current.text
        assert "expired at the end of October 2026" in current.text
        assert "gw-warning-banner" in current.text
        past = authed_client.get("/?month=1&year=2020")
        assert 'id="payment-methods-banner"' in past.text
        assert "October 2026" in past.text

    def test_continue_link_for_open_replacement(
        self, authed_client, db_session, sample_category
    ):
        from app.models import PaymentMethod, RecurringBill
        from app.services.payment_methods import start

        old = PaymentMethod(nickname="Joint account", kind="bank", owner="Jordan")
        new = PaymentMethod(nickname="Offset account", kind="bank", owner="Jordan")
        db_session.add_all([old, new])
        db_session.flush()
        db_session.add(
            RecurringBill(
                name="Rent",
                amount=100,
                debtor_provider="Landlord",
                start_date="2026-01-01",
                frequency="monthly",
                category_id=sample_category.id,
                next_due_date="2027-06-01",
                payment_method_id=old.id,
            )
        )
        db_session.commit()
        start(db_session, old.id, new_id=new.id)
        db_session.commit()
        page = authed_client.get("/?month=1&year=2020")
        assert "Continue" in page.text
        assert f"/payment-methods/{old.id}/replace" in page.text
        assert "bill moved" in page.text


class TestDueToConfirmCard:
    def test_shows_only_when_due_and_only_this_month(
        self, authed_client, db_session, sample_category, sample_budget_categories
    ):
        from datetime import timedelta

        from app.config import TIMEZONE
        from app.models import Budget, RecurringBill, SinkingFund

        today = datetime.now(TIMEZONE).date()
        yesterday = today - timedelta(days=1)
        fund = SinkingFund(
            name="Bills", color="#ff0000", current_balance=100, is_system=True
        )
        db_session.add(fund)
        groceries = sample_budget_categories[0]
        budget = Budget(
            category_id=groceries.id,
            month=yesterday.month,
            year=yesterday.year,
            allocated_amount=50,
            spent_amount=0,
            fund_balance=0,
        )
        db_session.add(budget)
        db_session.flush()
        due = RecurringBill(
            name="HelloFresh",
            amount=89.95,
            debtor_provider="HelloFresh",
            start_date="2020-01-01",
            frequency="weekly",
            category_id=groceries.id,
            next_due_date=yesterday.isoformat(),
            paid_from="budget",
        )
        variable = RecurringBill(
            name="Water",
            amount=210,
            debtor_provider="Utility",
            start_date="2020-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date=yesterday.isoformat(),
            bill_type="variable",
            paid_from="bills_fund",
        )
        fixed = RecurringBill(
            name="Rent",
            amount=1000,
            debtor_provider="Landlord",
            start_date="2020-01-01",
            frequency="monthly",
            category_id=sample_category.id,
            next_due_date=yesterday.isoformat(),
            bill_type="fixed",
            paid_from="bills_fund",
        )
        db_session.add_all([due, variable, fixed])
        db_session.commit()
        page = authed_client.get("/")
        assert 'id="due-to-confirm"' in page.text
        assert "HelloFresh" in page.text
        assert "Water" in page.text
        assert "Groceries budget" in page.text
        assert (
            ">Rent<" not in page.text.split('id="due-to-confirm"')[1].split("</div>")[0]
        )
        past = authed_client.get("/?month=1&year=2020")
        assert 'id="due-to-confirm"' not in past.text
        confirmed = authed_client.post(
            f"/bills/{due.id}/confirm?from=dashboard",
            data={"amount": "90.00", "due_date": due.next_due_date},
            headers={"x-csrftoken": authed_client.csrf_token},
        )
        assert confirmed.status_code == 200
        assert "Water" in confirmed.text
        assert "HelloFresh" not in confirmed.text

    def test_problem_row_links_and_hides_confirm(
        self, authed_client, db_session, sample_budget_categories
    ):
        from app.models import RecurringBill

        groceries = sample_budget_categories[0]
        bill = RecurringBill(
            name="HelloFresh",
            amount=20,
            debtor_provider="HelloFresh",
            start_date="2020-01-01",
            frequency="weekly",
            category_id=groceries.id,
            next_due_date="2020-01-06",
            paid_from="budget",
        )
        db_session.add(bill)
        db_session.commit()
        page = authed_client.get("/")
        assert "no Groceries budget for January 2020" in page.text
        assert "Set it on the Budgets page" in page.text
        assert "month=1" in page.text and "year=2020" in page.text
        row = page.text.split("HelloFresh", 1)[1]
        assert "Skip" in row
        assert ">Confirm<" not in row.split("Skip", 1)[0]
