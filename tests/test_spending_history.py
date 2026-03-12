from decimal import Decimal


from app.models import Category, Transaction
from app.routes.spending_history import _build_spending_matrix


class TestSpendingHistoryPage:
    def test_renders_page(self, authed_client):
        response = authed_client.get("/spending-history")
        assert response.status_code == 200
        assert "Spending History" in response.text

    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/spending-history", follow_redirects=False)
        assert response.status_code == 303
        assert "/login" in response.headers["location"]

    def test_shows_year_navigation(self, authed_client):
        response = authed_client.get("/spending-history?year=2025")
        assert response.status_code == 200
        assert "2025" in response.text
        assert "2024" in response.text  # prev
        assert "2026" in response.text  # next

    def test_shows_all_twelve_months(self, authed_client, db_session):
        db_session.add(
            Category(
                name="Food", type="expense", color="#FF0000", is_budget_category=False
            )
        )
        db_session.commit()
        response = authed_client.get("/spending-history?year=2026")
        assert response.status_code == 200
        for month_abbr in [
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ]:
            assert month_abbr in response.text

    def test_shows_category_columns(self, authed_client, db_session):
        cat = Category(
            name="Dining", type="expense", color="#FF5500", is_budget_category=False
        )
        db_session.add(cat)
        db_session.commit()

        response = authed_client.get("/spending-history?year=2026")
        assert response.status_code == 200
        assert "Dining" in response.text

    def test_shows_spending_amounts(self, authed_client, db_session):
        cat = Category(
            name="Groceries", type="expense", color="#22C55E", is_budget_category=True
        )
        db_session.add(cat)
        db_session.commit()
        db_session.add(
            Transaction(
                date="2026-03-10",
                description="Supermarket",
                amount=123.45,
                category_id=cat.id,
                type="expense",
                transaction_type="budget_expense",
            )
        )
        db_session.commit()

        response = authed_client.get("/spending-history?year=2026")
        assert response.status_code == 200
        assert "123.45" in response.text

    def test_excludes_income_transactions(self, authed_client, db_session):
        income_cat = Category(
            name="Salary", type="income", color="#00FF00", is_budget_category=False
        )
        expense_cat = Category(
            name="Rent", type="expense", color="#FF0000", is_budget_category=False
        )
        db_session.add_all([income_cat, expense_cat])
        db_session.commit()
        db_session.add_all(
            [
                Transaction(
                    date="2026-01-01",
                    description="Pay cheque",
                    amount=5000.00,
                    category_id=income_cat.id,
                    type="income",
                    transaction_type="income",
                ),
                Transaction(
                    date="2026-01-05",
                    description="Rent payment",
                    amount=2400.00,
                    category_id=expense_cat.id,
                    type="expense",
                    transaction_type="regular",
                ),
            ]
        )
        db_session.commit()

        response = authed_client.get("/spending-history?year=2026")
        assert "2,400.00" in response.text
        assert "5,000.00" not in response.text

    def test_excludes_non_regular_budget_expense_types(self, authed_client, db_session):
        cat = Category(
            name="Savings Transfer",
            type="expense",
            color="#888",
            is_budget_category=False,
        )
        db_session.add(cat)
        db_session.commit()
        # income_allocation and withdrawal should be excluded
        db_session.add_all(
            [
                Transaction(
                    date="2026-02-01",
                    amount=300.00,
                    category_id=cat.id,
                    type="expense",
                    transaction_type="income_allocation",
                ),
                Transaction(
                    date="2026-02-01",
                    amount=200.00,
                    category_id=cat.id,
                    type="expense",
                    transaction_type="withdrawal",
                ),
            ]
        )
        db_session.commit()

        response = authed_client.get("/spending-history?year=2026")
        assert "300.00" not in response.text
        assert "200.00" not in response.text

    def test_links_cells_to_transactions_page(self, authed_client, db_session):
        cat = Category(
            name="Transport", type="expense", color="#3B82F6", is_budget_category=True
        )
        db_session.add(cat)
        db_session.commit()
        db_session.add(
            Transaction(
                date="2026-06-15",
                amount=55.00,
                category_id=cat.id,
                type="expense",
                transaction_type="budget_expense",
            )
        )
        db_session.commit()

        response = authed_client.get("/spending-history?year=2026")
        assert f"category_id={cat.id}" in response.text
        assert "month=6" in response.text

    def test_empty_state_no_categories(self, authed_client):
        response = authed_client.get("/spending-history?year=2026")
        assert response.status_code == 200
        assert "No expense categories found" in response.text

    def test_default_year_is_current(self, authed_client):
        response = authed_client.get("/spending-history")
        assert response.status_code == 200
        assert "2026" in response.text  # current year per test date

    def test_income_categories_not_shown_as_columns(self, authed_client, db_session):
        db_session.add(
            Category(
                name="Salary", type="income", color="#00FF00", is_budget_category=False
            )
        )
        db_session.add(
            Category(
                name="Rent", type="expense", color="#FF0000", is_budget_category=False
            )
        )
        db_session.commit()

        response = authed_client.get("/spending-history?year=2026")
        # Rent should appear as a column; Salary should not
        # (Salary may appear in the nav/page text so check more specifically)
        assert "Rent" in response.text
        # The income category should not appear as a table header
        assert 'title="Salary"' not in response.text


class TestBuildSpendingMatrix:
    def test_empty_returns_zeros(self, db_session):
        cat = Category(
            name="Food", type="expense", color="#FF0000", is_budget_category=False
        )
        db_session.add(cat)
        db_session.commit()

        matrix, row_totals, col_totals, grand_total = _build_spending_matrix(
            db_session, 2026, [cat]
        )

        assert grand_total == Decimal("0.00")
        assert all(v == Decimal("0.00") for v in row_totals.values())
        assert col_totals[cat.id] == Decimal("0.00")

    def test_aggregates_by_month_and_category(self, db_session):
        cat = Category(
            name="Food", type="expense", color="#FF0000", is_budget_category=False
        )
        db_session.add(cat)
        db_session.commit()
        db_session.add_all(
            [
                Transaction(
                    date="2026-01-05",
                    amount=50.00,
                    category_id=cat.id,
                    type="expense",
                    transaction_type="regular",
                ),
                Transaction(
                    date="2026-01-20",
                    amount=30.00,
                    category_id=cat.id,
                    type="expense",
                    transaction_type="budget_expense",
                ),
                Transaction(
                    date="2026-03-10",
                    amount=75.00,
                    category_id=cat.id,
                    type="expense",
                    transaction_type="regular",
                ),
            ]
        )
        db_session.commit()

        matrix, row_totals, col_totals, grand_total = _build_spending_matrix(
            db_session, 2026, [cat]
        )

        assert matrix[1][cat.id] == Decimal("80.00")
        assert matrix[3][cat.id] == Decimal("75.00")
        assert row_totals[1] == Decimal("80.00")
        assert row_totals[3] == Decimal("75.00")
        assert col_totals[cat.id] == Decimal("155.00")
        assert grand_total == Decimal("155.00")

    def test_excludes_wrong_year(self, db_session):
        cat = Category(
            name="Food", type="expense", color="#FF0000", is_budget_category=False
        )
        db_session.add(cat)
        db_session.commit()
        db_session.add(
            Transaction(
                date="2025-06-01",
                amount=99.00,
                category_id=cat.id,
                type="expense",
                transaction_type="regular",
            )
        )
        db_session.commit()

        _, _, col_totals, grand_total = _build_spending_matrix(db_session, 2026, [cat])
        assert grand_total == Decimal("0.00")
        assert col_totals[cat.id] == Decimal("0.00")

    def test_excludes_income_transactions(self, db_session):
        cat = Category(
            name="Salary", type="income", color="#00FF00", is_budget_category=False
        )
        db_session.add(cat)
        db_session.commit()
        db_session.add(
            Transaction(
                date="2026-01-01",
                amount=5000.00,
                category_id=cat.id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.commit()

        _, _, _, grand_total = _build_spending_matrix(db_session, 2026, [cat])
        assert grand_total == Decimal("0.00")
