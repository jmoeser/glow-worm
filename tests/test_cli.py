"""
Tests for the glow CLI.

Uses typer.testing.CliRunner to invoke commands and respx to mock httpx calls.
The conftest setup_database fixture runs (autouse) but is a no-op for these tests.
"""

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from typer.testing import CliRunner

from app.cli.main import app

runner = CliRunner()

SERVER_URL = "http://testserver"
FAKE_CONFIG = {"url": SERVER_URL, "api_key": "test-key-abc123"}


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config():
    """Patch require_config so commands don't need a real config file."""
    with patch("app.cli.client.require_config", return_value=FAKE_CONFIG):
        yield


# ---------------------------------------------------------------------------
# Sample API payloads
# ---------------------------------------------------------------------------

DASHBOARD_RESPONSE = {
    "total_income": "5000.00",
    "total_expenses": "1200.00",
    "net": "3800.00",
    "unallocated_income": "200.00",
    "budget_total_allocated": "1000.00",
    "budget_total_spent": "500.00",
    "budget_total_remaining": "500.00",
    "total_sinking_funds": "3000.00",
    "total_net_worth": "3700.00",
    "month_name": "February",
    "year": 2026,
    "sinking_funds": [
        {"name": "Bills", "current_balance": "2000.00", "monthly_allocation": "500.00"},
        {
            "name": "Savings",
            "current_balance": "1000.00",
            "monthly_allocation": "200.00",
        },
    ],
    "recent_transactions": [
        {
            "date": "2026-02-15",
            "description": "Groceries",
            "amount": "85.00",
            "type": "expense",
        },
    ],
}

SAMPLE_TRANSACTIONS = [
    {
        "id": 1,
        "date": "2026-02-15",
        "description": "Groceries",
        "amount": "85.00",
        "type": "expense",
        "transaction_type": "regular",
        "category_id": 1,
        "is_paid": True,
        "created_at": "2026-02-15T10:00:00",
    },
    {
        "id": 2,
        "date": "2026-02-01",
        "description": "Salary",
        "amount": "5000.00",
        "type": "income",
        "transaction_type": "income",
        "category_id": 2,
        "is_paid": True,
        "created_at": "2026-02-01T09:00:00",
    },
]

SAMPLE_BILLS = [
    {
        "id": 1,
        "name": "Rent",
        "amount": "2400.00",
        "debtor_provider": "Landlord",
        "start_date": "2026-01-01",
        "frequency": "monthly",
        "next_due_date": "2026-03-01",
        "is_active": True,
        "bill_type": "fixed",
        "category_id": 1,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
]

SAMPLE_FUNDS = [
    {
        "id": 1,
        "name": "Bills",
        "current_balance": "2000.00",
        "monthly_allocation": "500.00",
        "description": "For recurring bills",
        "color": "#FF0000",
        "is_deleted": False,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
]

SAMPLE_CATEGORIES = [
    {
        "id": 1,
        "name": "Groceries",
        "type": "expense",
        "color": "#22c55e",
        "is_budget_category": True,
        "is_deleted": False,
        "is_system": False,
    },
    {
        "id": 2,
        "name": "Salary",
        "type": "income",
        "color": "#3b82f6",
        "is_budget_category": False,
        "is_deleted": False,
        "is_system": True,
    },
]

SAMPLE_BUDGETS = [
    {
        "id": 1,
        "category_id": 1,
        "month": 2,
        "year": 2026,
        "allocated_amount": "600.00",
        "spent_amount": "150.00",
        "fund_balance": "0.00",
        "created_at": "2026-02-01T00:00:00",
        "updated_at": "2026-02-01T00:00:00",
    }
]


# ---------------------------------------------------------------------------
# Config commands
# ---------------------------------------------------------------------------


class TestConfigCommands:
    def test_set_url_writes_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        result = runner.invoke(app, ["config", "set-url", "http://localhost:8000"])
        assert result.exit_code == 0
        assert "http://localhost:8000" in result.output
        assert 'url = "http://localhost:8000"' in config_file.read_text()

    def test_set_url_strips_trailing_slash(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        runner.invoke(app, ["config", "set-url", "http://localhost:8000/"])
        assert 'url = "http://localhost:8000"' in config_file.read_text()

    def test_set_key_writes_config(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        result = runner.invoke(app, ["config", "set-key", "myapikey123"])
        assert result.exit_code == 0
        assert 'api_key = "myapikey123"' in config_file.read_text()

    def test_set_url_preserves_existing_api_key(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('url = "http://old"\napi_key = "existingkey"\n')
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        runner.invoke(app, ["config", "set-url", "http://new"])
        text = config_file.read_text()
        assert 'url = "http://new"' in text
        assert 'api_key = "existingkey"' in text

    def test_show_masks_api_key(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            'url = "http://localhost:8000"\napi_key = "supersecretkey"\n'
        )
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "http://localhost:8000" in result.output
        assert "supers..." in result.output
        assert "supersecretkey" not in result.output

    def test_show_not_configured(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "(not set)" in result.output


# ---------------------------------------------------------------------------
# Missing config guard
# ---------------------------------------------------------------------------


class TestMissingConfig:
    def test_exits_when_no_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", tmp_path / "config.toml")
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        result = runner.invoke(app, ["bills", "list"])
        assert result.exit_code == 1
        assert "Missing config" in result.output

    def test_exits_when_url_missing(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('api_key = "somekey"\n')
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        result = runner.invoke(app, ["bills", "list"])
        assert result.exit_code == 1
        assert "url" in result.output

    def test_exits_when_api_key_missing(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('url = "http://localhost:8000"\n')
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        result = runner.invoke(app, ["bills", "list"])
        assert result.exit_code == 1
        assert "api_key" in result.output


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class TestDashboard:
    def test_shows_summary_figures(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/dashboard").mock(
                return_value=httpx.Response(200, json=DASHBOARD_RESPONSE)
            )
            result = runner.invoke(app, ["dashboard"])
        assert result.exit_code == 0
        assert "February 2026" in result.output
        assert "5000.00" in result.output
        assert "3700.00" in result.output

    def test_shows_sinking_funds_table(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/dashboard").mock(
                return_value=httpx.Response(200, json=DASHBOARD_RESPONSE)
            )
            result = runner.invoke(app, ["dashboard"])
        assert "Bills" in result.output
        assert "Savings" in result.output
        assert "2000.00" in result.output

    def test_shows_recent_transactions(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/dashboard").mock(
                return_value=httpx.Response(200, json=DASHBOARD_RESPONSE)
            )
            result = runner.invoke(app, ["dashboard"])
        assert "Groceries" in result.output

    def test_passes_month_year_query_params(self, mock_config):
        with respx.mock:
            route = respx.get(f"{SERVER_URL}/api/dashboard").mock(
                return_value=httpx.Response(200, json=DASHBOARD_RESPONSE)
            )
            runner.invoke(app, ["dashboard", "--month", "1", "--year", "2026"])
        assert route.called
        assert "month=1" in str(route.calls[0].request.url)
        assert "year=2026" in str(route.calls[0].request.url)

    def test_sends_bearer_token(self, mock_config):
        with respx.mock:
            route = respx.get(f"{SERVER_URL}/api/dashboard").mock(
                return_value=httpx.Response(200, json=DASHBOARD_RESPONSE)
            )
            runner.invoke(app, ["dashboard"])
        assert (
            route.calls[0].request.headers["authorization"] == "Bearer test-key-abc123"
        )


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class TestTransactions:
    def test_list_shows_table(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/transactions").mock(
                return_value=httpx.Response(200, json=SAMPLE_TRANSACTIONS)
            )
            result = runner.invoke(app, ["tx", "list"])
        assert result.exit_code == 0
        assert "Groceries" in result.output
        assert "Salary" in result.output
        assert "85.00" in result.output

    def test_list_empty(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/transactions").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = runner.invoke(app, ["tx", "list"])
        assert result.exit_code == 0
        assert "No transactions found" in result.output

    def test_list_respects_limit(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/transactions").mock(
                return_value=httpx.Response(200, json=SAMPLE_TRANSACTIONS)
            )
            result = runner.invoke(app, ["tx", "list", "--limit", "1"])
        assert "Groceries" in result.output
        assert "Salary" not in result.output

    def test_list_passes_type_filter(self, mock_config):
        with respx.mock:
            route = respx.get(f"{SERVER_URL}/api/transactions").mock(
                return_value=httpx.Response(200, json=[SAMPLE_TRANSACTIONS[1]])
            )
            runner.invoke(app, ["tx", "list", "--type", "income"])
        assert "type_filter=income" in str(route.calls[0].request.url)

    def test_add_posts_correct_payload(self, mock_config):
        created = {**SAMPLE_TRANSACTIONS[0], "id": 3}
        with respx.mock:
            route = respx.post(f"{SERVER_URL}/api/transactions").mock(
                return_value=httpx.Response(201, json=created)
            )
            result = runner.invoke(
                app,
                [
                    "tx",
                    "add",
                    "--amount",
                    "85.00",
                    "--category-id",
                    "1",
                    "--type",
                    "expense",
                ],
            )
        assert result.exit_code == 0
        assert "#3" in result.output
        body = json.loads(route.calls[0].request.content)
        assert body["amount"] == 85.0
        assert body["category_id"] == 1
        assert body["type"] == "expense"
        assert "date" in body  # defaults to today

    def test_add_includes_optional_fields(self, mock_config):
        created = {**SAMPLE_TRANSACTIONS[0], "id": 4}
        with respx.mock:
            route = respx.post(f"{SERVER_URL}/api/transactions").mock(
                return_value=httpx.Response(201, json=created)
            )
            runner.invoke(
                app,
                [
                    "tx",
                    "add",
                    "--amount",
                    "50",
                    "--category-id",
                    "1",
                    "--type",
                    "expense",
                    "--description",
                    "Coffee",
                    "--fund-id",
                    "2",
                    "--date",
                    "2026-02-20",
                ],
            )
        body = json.loads(route.calls[0].request.content)
        assert body["description"] == "Coffee"
        assert body["sinking_fund_id"] == 2
        assert body["date"] == "2026-02-20"

    def test_delete_prompts_and_confirms(self, mock_config):
        with respx.mock:
            route = respx.delete(f"{SERVER_URL}/api/transactions/1").mock(
                return_value=httpx.Response(200, json={"detail": "Deleted"})
            )
            result = runner.invoke(app, ["tx", "delete", "1"], input="y\n")
        assert result.exit_code == 0
        assert route.called

    def test_delete_aborts_on_no(self, mock_config):
        with respx.mock:
            route = respx.delete(f"{SERVER_URL}/api/transactions/1").mock(
                return_value=httpx.Response(200, json={"detail": "Deleted"})
            )
            runner.invoke(app, ["tx", "delete", "1"], input="n\n")
        assert not route.called

    def test_delete_skips_prompt_with_yes_flag(self, mock_config):
        with respx.mock:
            route = respx.delete(f"{SERVER_URL}/api/transactions/1").mock(
                return_value=httpx.Response(200, json={"detail": "Deleted"})
            )
            result = runner.invoke(app, ["tx", "delete", "1", "--yes"])
        assert result.exit_code == 0
        assert route.called


# ---------------------------------------------------------------------------
# Bills
# ---------------------------------------------------------------------------


class TestBills:
    def test_list_shows_bills(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/bills").mock(
                return_value=httpx.Response(200, json=SAMPLE_BILLS)
            )
            result = runner.invoke(app, ["bills", "list"])
        assert result.exit_code == 0
        assert "Rent" in result.output
        assert "Landlord" in result.output
        assert "2400.00" in result.output

    def test_list_empty(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/bills").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = runner.invoke(app, ["bills", "list"])
        assert "No bills found" in result.output

    def test_pay_fetches_default_amount(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/bills/1").mock(
                return_value=httpx.Response(200, json=SAMPLE_BILLS[0])
            )
            pay_route = respx.post(f"{SERVER_URL}/api/bills/1/pay").mock(
                return_value=httpx.Response(200, json={"detail": "ok"})
            )
            result = runner.invoke(app, ["bills", "pay", "1"], input="y\n")
        assert result.exit_code == 0
        assert pay_route.called
        body = json.loads(pay_route.calls[0].request.content)
        assert body["amount"] == 2400.0

    def test_pay_accepts_override_amount(self, mock_config):
        with respx.mock:
            pay_route = respx.post(f"{SERVER_URL}/api/bills/1/pay").mock(
                return_value=httpx.Response(200, json={"detail": "ok"})
            )
            runner.invoke(app, ["bills", "pay", "1", "--amount", "100.00"], input="y\n")
        body = json.loads(pay_route.calls[0].request.content)
        assert body["amount"] == 100.0

    def test_pay_skips_bill_fetch_when_amount_provided(self, mock_config):
        with respx.mock:
            get_route = respx.get(f"{SERVER_URL}/api/bills/1").mock(
                return_value=httpx.Response(200, json=SAMPLE_BILLS[0])
            )
            respx.post(f"{SERVER_URL}/api/bills/1/pay").mock(
                return_value=httpx.Response(200, json={"detail": "ok"})
            )
            runner.invoke(app, ["bills", "pay", "1", "--amount", "100"], input="y\n")
        assert not get_route.called

    def test_add_posts_correct_payload(self, mock_config):
        with respx.mock:
            route = respx.post(f"{SERVER_URL}/api/bills").mock(
                return_value=httpx.Response(201, json=SAMPLE_BILLS[0])
            )
            runner.invoke(
                app,
                [
                    "bills",
                    "add",
                    "--name",
                    "Internet",
                    "--amount",
                    "89",
                    "--provider",
                    "ISP Co",
                    "--frequency",
                    "monthly",
                    "--category-id",
                    "1",
                    "--start-date",
                    "2026-01-01",
                    "--next-due-date",
                    "2026-03-01",
                ],
            )
        body = json.loads(route.calls[0].request.content)
        assert body["name"] == "Internet"
        assert body["debtor_provider"] == "ISP Co"
        assert body["frequency"] == "monthly"
        assert body["bill_type"] == "fixed"

    def test_delete_deactivates_bill(self, mock_config):
        with respx.mock:
            route = respx.delete(f"{SERVER_URL}/api/bills/1").mock(
                return_value=httpx.Response(200, json={"detail": "deactivated"})
            )
            result = runner.invoke(app, ["bills", "delete", "1", "--yes"])
        assert result.exit_code == 0
        assert route.called

    def test_delete_prompts_without_yes_flag(self, mock_config):
        with respx.mock:
            route = respx.delete(f"{SERVER_URL}/api/bills/1").mock(
                return_value=httpx.Response(200, json={"detail": "deactivated"})
            )
            runner.invoke(app, ["bills", "delete", "1"], input="n\n")
        assert not route.called


# ---------------------------------------------------------------------------
# Sinking Funds
# ---------------------------------------------------------------------------


class TestFunds:
    def test_list_shows_funds(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/sinking-funds").mock(
                return_value=httpx.Response(200, json=SAMPLE_FUNDS)
            )
            result = runner.invoke(app, ["funds", "list"])
        assert result.exit_code == 0
        assert "Bills" in result.output
        assert "2000.00" in result.output
        assert "500.00" in result.output

    def test_list_empty(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/sinking-funds").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = runner.invoke(app, ["funds", "list"])
        assert "No sinking funds found" in result.output

    def test_add_posts_correct_payload(self, mock_config):
        created = {**SAMPLE_FUNDS[0], "id": 2, "name": "Emergency"}
        with respx.mock:
            route = respx.post(f"{SERVER_URL}/api/sinking-funds").mock(
                return_value=httpx.Response(201, json=created)
            )
            result = runner.invoke(
                app,
                [
                    "funds",
                    "add",
                    "--name",
                    "Emergency",
                    "--monthly-allocation",
                    "300.00",
                    "--color",
                    "#3b82f6",
                ],
            )
        assert result.exit_code == 0
        body = json.loads(route.calls[0].request.content)
        assert body["name"] == "Emergency"
        assert body["color"] == "#3b82f6"
        assert body["monthly_allocation"] == 300.0
        assert body["current_balance"] == 0.0

    def test_add_passes_optional_description(self, mock_config):
        with respx.mock:
            route = respx.post(f"{SERVER_URL}/api/sinking-funds").mock(
                return_value=httpx.Response(201, json=SAMPLE_FUNDS[0])
            )
            runner.invoke(
                app,
                [
                    "funds",
                    "add",
                    "--name",
                    "Holidays",
                    "--monthly-allocation",
                    "100",
                    "--color",
                    "#aabbcc",
                    "--description",
                    "Annual leave savings",
                ],
            )
        body = json.loads(route.calls[0].request.content)
        assert body["description"] == "Annual leave savings"

    def test_delete_soft_deletes(self, mock_config):
        with respx.mock:
            route = respx.delete(f"{SERVER_URL}/api/sinking-funds/1").mock(
                return_value=httpx.Response(200, json={"detail": "deleted"})
            )
            result = runner.invoke(app, ["funds", "delete", "1", "--yes"])
        assert result.exit_code == 0
        assert route.called


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


class TestBudgets:
    def test_list_shows_budgets(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/budgets").mock(
                return_value=httpx.Response(200, json=SAMPLE_BUDGETS)
            )
            result = runner.invoke(app, ["budgets", "list"])
        assert result.exit_code == 0
        assert "600.00" in result.output
        assert "150.00" in result.output
        assert "2/2026" in result.output

    def test_list_shows_remaining_in_green_when_positive(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/budgets").mock(
                return_value=httpx.Response(200, json=SAMPLE_BUDGETS)
            )
            result = runner.invoke(app, ["budgets", "list"])
        assert "450.00" in result.output  # 600 - 150

    def test_list_empty(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/budgets").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = runner.invoke(app, ["budgets", "list"])
        assert "No budgets found" in result.output

    def test_list_passes_month_year_params(self, mock_config):
        with respx.mock:
            route = respx.get(f"{SERVER_URL}/api/budgets").mock(
                return_value=httpx.Response(200, json=SAMPLE_BUDGETS)
            )
            runner.invoke(app, ["budgets", "list", "--month", "2", "--year", "2026"])
        assert "month=2" in str(route.calls[0].request.url)
        assert "year=2026" in str(route.calls[0].request.url)

    def test_add_posts_correct_payload(self, mock_config):
        created = {**SAMPLE_BUDGETS[0], "id": 2}
        with respx.mock:
            route = respx.post(f"{SERVER_URL}/api/budgets").mock(
                return_value=httpx.Response(201, json=created)
            )
            result = runner.invoke(
                app,
                [
                    "budgets",
                    "add",
                    "--category-id",
                    "1",
                    "--allocated-amount",
                    "600.00",
                    "--month",
                    "2",
                    "--year",
                    "2026",
                ],
            )
        assert result.exit_code == 0
        body = json.loads(route.calls[0].request.content)
        assert body["category_id"] == 1
        assert body["allocated_amount"] == 600.0
        assert body["month"] == 2
        assert body["year"] == 2026

    def test_delete_budget(self, mock_config):
        with respx.mock:
            route = respx.delete(f"{SERVER_URL}/api/budgets/1").mock(
                return_value=httpx.Response(200, json={"detail": "deleted"})
            )
            result = runner.invoke(app, ["budgets", "delete", "1", "--yes"])
        assert result.exit_code == 0
        assert route.called


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


class TestCategories:
    def test_list_shows_categories(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/categories").mock(
                return_value=httpx.Response(200, json=SAMPLE_CATEGORIES)
            )
            result = runner.invoke(app, ["categories", "list"])
        assert result.exit_code == 0
        assert "Groceries" in result.output
        assert "expense" in result.output
        assert "Salary" in result.output
        assert "income" in result.output

    def test_list_shows_system_flag(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/categories").mock(
                return_value=httpx.Response(200, json=SAMPLE_CATEGORIES)
            )
            result = runner.invoke(app, ["categories", "list"])
        assert result.exit_code == 0
        assert "Yes" in result.output  # is_system for Salary

    def test_list_empty(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/categories").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = runner.invoke(app, ["categories", "list"])
        assert "No categories found" in result.output

    def test_add_posts_correct_payload(self, mock_config):
        created = {**SAMPLE_CATEGORIES[0], "id": 3, "name": "Dining Out"}
        with respx.mock:
            route = respx.post(f"{SERVER_URL}/api/categories").mock(
                return_value=httpx.Response(201, json=created)
            )
            result = runner.invoke(
                app,
                [
                    "categories",
                    "add",
                    "--name",
                    "Dining Out",
                    "--type",
                    "expense",
                    "--color",
                    "#f59e0b",
                ],
            )
        assert result.exit_code == 0
        body = json.loads(route.calls[0].request.content)
        assert body["name"] == "Dining Out"
        assert body["type"] == "expense"
        assert body["color"] == "#f59e0b"
        assert body["is_budget_category"] is False

    def test_add_with_budget_flag(self, mock_config):
        created = {
            **SAMPLE_CATEGORIES[0],
            "id": 4,
            "name": "Transport",
            "is_budget_category": True,
        }
        with respx.mock:
            route = respx.post(f"{SERVER_URL}/api/categories").mock(
                return_value=httpx.Response(201, json=created)
            )
            runner.invoke(
                app,
                [
                    "categories",
                    "add",
                    "--name",
                    "Transport",
                    "--type",
                    "expense",
                    "--color",
                    "#6366f1",
                    "--is-budget-category",
                ],
            )
        body = json.loads(route.calls[0].request.content)
        assert body["is_budget_category"] is True

    def test_delete_soft_deletes(self, mock_config):
        with respx.mock:
            route = respx.delete(f"{SERVER_URL}/api/categories/1").mock(
                return_value=httpx.Response(200, json={"detail": "deleted"})
            )
            result = runner.invoke(app, ["categories", "delete", "1", "--yes"])
        assert result.exit_code == 0
        assert route.called

    def test_list_json(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/categories").mock(
                return_value=httpx.Response(200, json=SAMPLE_CATEGORIES)
            )
            result = runner.invoke(app, ["categories", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["name"] == "Groceries"


# ---------------------------------------------------------------------------
# --json flag
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_bills_list_json(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/bills").mock(
                return_value=httpx.Response(200, json=SAMPLE_BILLS)
            )
            result = runner.invoke(app, ["bills", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert data[0]["name"] == "Rent"
        assert data[0]["amount"] == "2400.00"

    def test_bills_list_json_empty(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/bills").mock(
                return_value=httpx.Response(200, json=[])
            )
            result = runner.invoke(app, ["bills", "list", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_tx_list_json(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/transactions").mock(
                return_value=httpx.Response(200, json=SAMPLE_TRANSACTIONS)
            )
            result = runner.invoke(app, ["tx", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data) == 2
        assert data[0]["description"] == "Groceries"

    def test_tx_add_json(self, mock_config):
        created = {**SAMPLE_TRANSACTIONS[0], "id": 10}
        with respx.mock:
            respx.post(f"{SERVER_URL}/api/transactions").mock(
                return_value=httpx.Response(201, json=created)
            )
            result = runner.invoke(
                app,
                [
                    "tx",
                    "add",
                    "--amount",
                    "50",
                    "--category-id",
                    "1",
                    "--type",
                    "expense",
                    "--json",
                ],
            )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == 10

    def test_dashboard_json(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/dashboard").mock(
                return_value=httpx.Response(200, json=DASHBOARD_RESPONSE)
            )
            result = runner.invoke(app, ["dashboard", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total_income"] == "5000.00"
        assert data["month_name"] == "February"

    def test_funds_list_json(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/sinking-funds").mock(
                return_value=httpx.Response(200, json=SAMPLE_FUNDS)
            )
            result = runner.invoke(app, ["funds", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["name"] == "Bills"

    def test_budgets_list_json(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/budgets").mock(
                return_value=httpx.Response(200, json=SAMPLE_BUDGETS)
            )
            result = runner.invoke(app, ["budgets", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data[0]["allocated_amount"] == "600.00"

    def test_config_show_json(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text('url = "http://localhost:8000"\napi_key = "mykey"\n')
        monkeypatch.setattr("app.cli.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("app.cli.config.CONFIG_DIR", tmp_path)
        result = runner.invoke(app, ["config", "show", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["url"] == "http://localhost:8000"
        assert data["api_key"] == "mykey"
        assert "config_file" in data

    def test_json_flag_produces_valid_json(self, mock_config):
        """Smoke test: --json output must always be parseable."""
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/bills").mock(
                return_value=httpx.Response(200, json=SAMPLE_BILLS)
            )
            result = runner.invoke(app, ["bills", "list", "--json"])
        json.loads(result.output)  # raises if invalid


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_non_2xx_prints_detail_and_exits(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/bills").mock(
                return_value=httpx.Response(401, json={"detail": "Unauthorized"})
            )
            result = runner.invoke(app, ["bills", "list"])
        assert result.exit_code == 1
        assert "401" in result.output
        assert "Unauthorized" in result.output

    def test_non_2xx_plain_text_body(self, mock_config):
        with respx.mock:
            respx.get(f"{SERVER_URL}/api/bills").mock(
                return_value=httpx.Response(500, text="Internal Server Error")
            )
            result = runner.invoke(app, ["bills", "list"])
        assert result.exit_code == 1
        assert "500" in result.output

    def test_404_on_delete(self, mock_config):
        with respx.mock:
            respx.delete(f"{SERVER_URL}/api/bills/99").mock(
                return_value=httpx.Response(404, json={"detail": "Bill not found"})
            )
            result = runner.invoke(app, ["bills", "delete", "99", "--yes"])
        assert result.exit_code == 1
        assert "Bill not found" in result.output
