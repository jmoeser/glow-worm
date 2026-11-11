"""Contracts register: plans, bill links, and payment stamps."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import TIMEZONE
from app.models import (
    Budget,
    Category,
    Contract,
    HouseholdMonth,
    IncomeAllocation,
    RecurringBill,
    SinkingFund,
    Transaction,
)
from app.services.recurring_payments import confirm_payment, pay_bills_fund
from app.tasks import process_due_bills

PAN = "4111111111111111"


@pytest.fixture(autouse=True)
def frozen_brisbane_today(monkeypatch):
    monkeypatch.setattr(
        "app.services.contracts.brisbane_today",
        lambda: date(2026, 11, 6),
    )


def _post(client, url, data, *, follow=False):
    return client.post(
        url,
        data=data,
        headers={"x-csrftoken": client.csrf_token},
        follow_redirects=follow,
    )


def _form(**kwargs):
    data = {
        "name": "Home electricity",
        "provider": "Origin",
        "plan_name": "Night Shift",
        "service": "electricity",
        "bill_delivery": "email",
        "delivery_detail": "bills@example.com",
        "notes": "Night rate",
        "started_on": "2024-01-01",
        "ended_on": "",
    }
    data.update(kwargs)
    return data


def _json(**kwargs):
    data = {
        "name": "Home electricity",
        "provider": "Origin",
        "plan_name": "Night Shift",
        "service": "electricity",
        "bill_delivery": "email",
        "delivery_detail": "bills@example.com",
        "notes": "Night rate",
        "started_on": "2024-01-01",
    }
    data.update(kwargs)
    return data


def _bill(db, category_id, **kwargs) -> RecurringBill:
    values = {
        "name": "Power",
        "amount": 40,
        "debtor_provider": "Origin",
        "start_date": "2026-01-01",
        "frequency": "monthly",
        "category_id": category_id,
        "next_due_date": "2026-06-01",
        "is_active": True,
        "paid_from": "bills_fund",
        "bill_type": "fixed",
    }
    values.update(kwargs)
    bill = RecurringBill(**values)
    db.add(bill)
    db.commit()
    db.refresh(bill)
    return bill


class TestContractCrud:
    def test_create_strips_and_lists_active_only(self, authed_client, db_session):
        created = _post(
            authed_client,
            "/contracts",
            _form(name="  Home electricity  ", provider="  Origin  "),
        )
        assert created.status_code == 303
        contract = db_session.query(Contract).one()
        assert contract.name == "Home electricity"
        assert contract.provider == "Origin"
        assert contract.plan_name == "Night Shift"
        assert contract.service == "electricity"
        assert contract.ended_on is None

        page = authed_client.get("/contracts")
        assert page.status_code == 200
        assert "Contract added." in page.text
        assert "Home electricity" in page.text
        assert "Health insurance" not in page.text or "Electricity" in page.text
        assert "Electricity" in page.text
        assert "Email" in page.text
        assert "Night Shift" in page.text
        assert "Active" in page.text
        assert 'href="/contracts?ended=1"' in page.text
        text = page.text
        assert (
            text.index('href="/bills"')
            < text.index('href="/contracts"')
            < text.index('href="/payment-methods"')
        )

        duplicate = authed_client.post(
            "/api/contracts", json=_json(name="Home electricity")
        )
        assert duplicate.status_code == 201
        assert duplicate.json()["name"] == "Home electricity"
        assert duplicate.json()["plan_name"] == "Night Shift"

        ended = authed_client.post(
            "/api/contracts",
            json=_json(name="Old plan", plan_name=None, ended_on="2024-05-01"),
        )
        assert ended.status_code == 201
        ended_id = ended.json()["id"]
        hidden = authed_client.get("/api/contracts")
        assert ended_id not in {row["id"] for row in hidden.json()}
        shown = authed_client.get("/api/contracts?include_ended=true")
        assert ended_id in {row["id"] for row in shown.json()}
        shown_flag = authed_client.get("/api/contracts?ended=1")
        assert ended_id in {row["id"] for row in shown_flag.json()}
        fetched = authed_client.get(f"/api/contracts/{ended_id}")
        assert fetched.status_code == 200
        assert fetched.json()["ended_on"] == "2024-05-01"

        html_ended = authed_client.get("/contracts")
        assert "Old plan" not in html_ended.text
        html_shown = authed_client.get("/contracts?ended=1")
        assert "Old plan" in html_shown.text
        assert "Ended 1 May 2024" in html_shown.text

    def test_partial_update_keeps_omitted_fields(self, authed_client, db_session):
        made = authed_client.post("/api/contracts", json=_json())
        contract_id = made.json()["id"]
        edited = authed_client.put(
            f"/api/contracts/{contract_id}",
            json={"plan_name": "  Night Shift  "},
        )
        assert edited.status_code == 200
        body = edited.json()
        assert body["plan_name"] == "Night Shift"
        assert body["provider"] == "Origin"
        assert body["notes"] == "Night rate"
        assert body["ended_on"] is None
        cleared = authed_client.put(
            f"/api/contracts/{contract_id}",
            json={"service": None, "bill_delivery": None},
        )
        assert cleared.status_code == 422
        db_session.expire_all()
        kept = db_session.get(Contract, contract_id)
        assert kept.service == "electricity"
        assert kept.bill_delivery == "email"

    def test_cannot_reopen_or_set_a_future_end(self, authed_client):
        made = authed_client.post(
            "/api/contracts",
            json=_json(ended_on="2024-06-01"),
        )
        contract_id = made.json()["id"]
        reopened = authed_client.put(
            f"/api/contracts/{contract_id}",
            json={"ended_on": None},
        )
        assert reopened.status_code == 422
        assert "stays ended" in reopened.text

        future = authed_client.post(
            "/api/contracts",
            json=_json(name="Later", ended_on="2099-01-01"),
        )
        assert future.status_code == 422
        assert "future" in future.text
        assert "2099-01-01" not in future.text

        ordered = authed_client.post(
            "/api/contracts",
            json=_json(
                name="Backwards",
                started_on="2024-06-01",
                ended_on="2024-01-01",
            ),
        )
        assert ordered.status_code == 422
        assert "on or after the start date" in ordered.text

    @pytest.mark.parametrize(
        "field",
        ["name", "provider", "plan_name", "delivery_detail", "notes"],
    )
    def test_digit_run_rejected_without_echo(self, authed_client, field):
        response = authed_client.post(
            "/api/contracts",
            json=_json(**{field: PAN}),
        )
        assert response.status_code == 422
        assert PAN not in response.text
        assert "input" not in response.text

    def test_invalid_service_delivery_and_calendar_date(self, authed_client):
        service = authed_client.post(
            "/api/contracts",
            json=_json(service="gas"),
        )
        assert service.status_code == 422
        delivery = authed_client.post(
            "/api/contracts",
            json=_json(bill_delivery="pigeon"),
        )
        assert delivery.status_code == 422
        calendar_date = authed_client.post(
            "/api/contracts",
            json=_json(started_on="2026-02-31"),
        )
        assert calendar_date.status_code == 422
        assert "2026-02-31" not in calendar_date.text
        assert "real date" in calendar_date.text

        html = _post(authed_client, "/contracts", _form(name=PAN))
        assert html.status_code == 422
        assert PAN not in html.text
        assert 'id="contract-error"' in html.text


class TestEndAndDelete:
    def test_end_uses_brisbane_today_and_leaves_bills(
        self, authed_client, db_session, sample_category, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.contracts.brisbane_today",
            lambda: date(2026, 3, 1),
        )
        made = authed_client.post("/api/contracts", json=_json())
        contract_id = made.json()["id"]
        bill = _bill(db_session, sample_category.id, contract_id=contract_id)
        due = bill.next_due_date

        ended = _post(authed_client, f"/contracts/{contract_id}/end", {})
        assert ended.status_code == 303
        db_session.refresh(bill)
        contract = db_session.get(Contract, contract_id)
        assert contract.ended_on == "2026-03-01"
        assert bill.is_active is True
        assert bill.next_due_date == due
        assert bill.contract_id == contract_id

        page = authed_client.get(f"/contracts/{contract_id}")
        assert "Ended 1 Mar 2026" in page.text
        assert (
            "Linked bills are unchanged and will still be paid / forecast as usual."
            in page.text
        )
        assert "for typos" in page.text
        assert "Payments already recorded stay off this plan." in page.text

    def test_delete_blocked_by_active_or_inactive_bill(
        self, authed_client, db_session, sample_category
    ):
        active = authed_client.post("/api/contracts", json=_json(name="Live")).json()
        paused = authed_client.post("/api/contracts", json=_json(name="Paused")).json()
        _bill(
            db_session, sample_category.id, name="Live bill", contract_id=active["id"]
        )
        inactive = _bill(
            db_session,
            sample_category.id,
            name="Old bill",
            contract_id=paused["id"],
            is_active=False,
        )

        blocked_active = authed_client.delete(f"/api/contracts/{active['id']}")
        assert blocked_active.status_code == 409
        assert "still links" in blocked_active.text
        blocked_inactive = authed_client.delete(f"/api/contracts/{paused['id']}")
        assert blocked_inactive.status_code == 409

        detail = authed_client.get(f"/contracts/{paused['id']}")
        assert "Inactive" in detail.text
        assert "Old bill" in detail.text
        assert "2 linked" not in detail.text
        assert "1 linked" in detail.text

        unlinked = _post(
            authed_client,
            f"/contracts/{paused['id']}/unlink",
            {"bill_id": str(inactive.id)},
        )
        assert unlinked.status_code == 303
        db_session.refresh(inactive)
        assert inactive.contract_id is None
        assert inactive.is_active is False
        assert inactive.next_due_date == "2026-06-01"

        removed = authed_client.delete(f"/api/contracts/{paused['id']}")
        assert removed.status_code == 200
        assert db_session.get(Contract, paused["id"]) is None

        html_blocked = _post(authed_client, f"/contracts/{active['id']}/delete", {})
        assert html_blocked.status_code == 409
        assert "still links" in html_blocked.text

    def test_html_edit_keeps_absent_fields_and_refuses_reopen(
        self, authed_client, db_session
    ):
        made = authed_client.post("/api/contracts", json=_json(ended_on="2024-06-01"))
        contract_id = made.json()["id"]
        posted = _form(provider="Origin Energy", ended_on="2024-06-01")
        del posted["plan_name"]
        saved = _post(authed_client, f"/contracts/{contract_id}", posted)
        assert saved.status_code == 303
        contract = db_session.get(Contract, contract_id)
        assert contract.provider == "Origin Energy"
        assert contract.plan_name == "Night Shift"
        assert contract.ended_on == "2024-06-01"

        reopened = _post(
            authed_client,
            f"/contracts/{contract_id}",
            _form(ended_on=""),
        )
        assert reopened.status_code == 422
        assert "stays ended" in reopened.text
        db_session.refresh(contract)
        assert contract.ended_on == "2024-06-01"
        missing = authed_client.get("/contracts/99999")
        assert missing.status_code == 404

    def test_delete_clears_payment_stamps_when_no_bill_remains(
        self, authed_client, db_session, sample_category
    ):
        made = authed_client.post("/api/contracts", json=_json(name="Gone")).json()
        txn = Transaction(
            date="2026-01-01",
            description="Old payment",
            amount=10,
            category_id=sample_category.id,
            type="expense",
            transaction_type="regular",
            contract_id=made["id"],
        )
        db_session.add(txn)
        db_session.commit()
        removed = authed_client.delete(f"/api/contracts/{made['id']}")
        assert removed.status_code == 200
        db_session.refresh(txn)
        assert txn.contract_id is None


class TestBillContractLink:
    def test_json_rules(self, authed_client, db_session, sample_category):
        active = authed_client.post("/api/contracts", json=_json(name="Current")).json()
        ended = authed_client.post(
            "/api/contracts",
            json=_json(name="Finished", ended_on="2024-02-01"),
        ).json()
        created = authed_client.post(
            "/api/bills",
            json={
                "name": "Power",
                "amount": "40.00",
                "debtor_provider": "Origin",
                "start_date": "2026-01-01",
                "frequency": "monthly",
                "category_id": sample_category.id,
                "next_due_date": "2026-06-01",
                "contract_id": active["id"],
            },
        )
        assert created.status_code == 201
        bill_id = created.json()["id"]
        assert created.json()["contract_id"] == active["id"]
        assert created.json()["debtor_provider"] == "Origin"

        renamed = authed_client.put(
            f"/api/bills/{bill_id}", json={"name": "Power bill"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["contract_id"] == active["id"]
        assert renamed.json()["name"] == "Power bill"

        missing = authed_client.put(
            f"/api/bills/{bill_id}",
            json={"contract_id": 99999},
        )
        assert missing.status_code == 422
        ended_link = authed_client.put(
            f"/api/bills/{bill_id}",
            json={"contract_id": ended["id"]},
        )
        assert ended_link.status_code == 422

        bill = db_session.get(RecurringBill, bill_id)
        bill.contract_id = ended["id"]
        db_session.commit()
        resaved = authed_client.put(
            f"/api/bills/{bill_id}",
            json={"contract_id": ended["id"], "name": "Power bill"},
        )
        assert resaved.status_code == 200
        assert resaved.json()["contract_id"] == ended["id"]

        cleared = authed_client.put(
            f"/api/bills/{bill_id}",
            json={"contract_id": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["contract_id"] is None

    def test_html_only_changes_contract_when_the_field_is_present(
        self, authed_client, db_session, sample_category
    ):
        active = authed_client.post("/api/contracts", json=_json()).json()
        ended = authed_client.post(
            "/api/contracts",
            json=_json(name="Finished", plan_name="", ended_on="2024-02-01"),
        ).json()
        bill = _bill(db_session, sample_category.id, contract_id=active["id"])

        kept = _post(
            authed_client,
            f"/bills/{bill.id}",
            {
                "name": "Power",
                "next_due_date": "2026-06-01",
                "next_due_date_was": "2026-06-01",
            },
        )
        assert kept.status_code == 200
        db_session.refresh(bill)
        assert bill.contract_id == active["id"]

        cleared = _post(
            authed_client,
            f"/bills/{bill.id}",
            {
                "name": "Power",
                "contract_id": "",
                "next_due_date": "2026-06-01",
                "next_due_date_was": "2026-06-01",
            },
        )
        assert cleared.status_code == 200
        db_session.refresh(bill)
        assert bill.contract_id is None

        rejected = _post(
            authed_client,
            f"/bills/{bill.id}",
            {
                "contract_id": str(ended["id"]),
                "next_due_date": "2026-06-01",
                "next_due_date_was": "2026-06-01",
            },
        )
        assert rejected.status_code == 422
        db_session.refresh(bill)
        assert bill.contract_id is None

        bill.contract_id = ended["id"]
        db_session.commit()
        resaved = _post(
            authed_client,
            f"/bills/{bill.id}",
            {
                "contract_id": str(ended["id"]),
                "next_due_date": "2026-06-01",
                "next_due_date_was": "2026-06-01",
            },
        )
        assert resaved.status_code == 200
        db_session.refresh(bill)
        assert bill.contract_id == ended["id"]

        page = authed_client.get("/bills")
        assert "Finished — Origin" in page.text
        assert ">Ended<" in page.text
        assert 'name="contract_id"' in page.text
        confirm = authed_client.get(f"/bills/{bill.id}/confirm")
        assert confirm.status_code == 200
        assert 'colspan="9"' in confirm.text
        edit = authed_client.get(f"/bills/{bill.id}/edit")
        assert "(Ended)" in edit.text
        assert 'name="contract_id"' in edit.text


class TestPaymentStamp:
    def test_confirm_pay_and_autopay_stamp_then_leave_history(
        self, db_session, sample_category, monkeypatch
    ):
        monkeypatch.setattr(
            "app.services.recurring_payments._today",
            lambda: date(2026, 11, 3),
        )
        monkeypatch.setattr("app.tasks._today", lambda: date(2026, 6, 1))
        groceries = Category(
            name="Groceries",
            type="expense",
            color="#22C55E",
            is_budget_category=True,
        )
        transfer = Category(
            name="Transfer", type="transfer", color="#888888", is_system=True
        )
        income = Category(name="Income", type="income", color="#00aa00", is_system=True)
        db_session.add_all([groceries, transfer, income])
        db_session.flush()
        bills_fund = SinkingFund(
            name="Bills", color="#ff0000", current_balance=500, is_system=True
        )
        other = SinkingFund(name="Kids fund", color="#00ff00", current_balance=80)
        overflow = SinkingFund(name="Overflow", color="#0000ff", current_balance=10)
        db_session.add_all([bills_fund, other, overflow])
        db_session.flush()
        october = Budget(
            category_id=groceries.id,
            month=10,
            year=2026,
            allocated_amount=400,
            spent_amount=0,
            fund_balance=0,
        )
        db_session.add(october)
        db_session.flush()
        old = Contract(
            name="Old",
            provider="Origin",
            service="electricity",
            bill_delivery="email",
        )
        new = Contract(
            name="New",
            provider="ENGIE",
            service="electricity",
            bill_delivery="app",
        )
        db_session.add_all([old, new])
        db_session.flush()

        budget_bill = _bill(
            db_session,
            groceries.id,
            name="Groceries box",
            paid_from="budget",
            next_due_date="2026-10-26",
            frequency="weekly",
            contract_id=old.id,
        )
        db_session.add(
            IncomeAllocation(
                monthly_income_amount=5000,
                monthly_budget_allocation=400,
                bills_fund_allocation_type="fixed",
                bills_fund_fixed_amount=0,
                overflow_sinking_fund_id=overflow.id,
            )
        )
        db_session.add(
            Transaction(
                date="2026-11-01",
                description="Monthly income",
                amount=1000,
                category_id=income.id,
                type="income",
                transaction_type="income",
            )
        )
        db_session.add(
            HouseholdMonth(
                year=2026,
                month=10,
                settlement_outcome="surplus",
                settlement_fund_id=overflow.id,
                settlement_amount=20,
                settled_on="2026-11-01",
            )
        )
        db_session.commit()
        confirmed = confirm_payment(
            db_session, budget_bill, Decimal("12.00"), "2026-10-26"
        )
        db_session.commit()
        assert confirmed.transaction.transaction_type == "budget_expense"
        assert confirmed.transaction.contract_id == old.id
        back = db_session.get(Transaction, confirmed.taken_back["transaction_id"])
        assert back.transaction_type == "withdrawal"
        assert back.recurring_bill_id == budget_bill.id
        assert back.contract_id is None

        fund_bill = _bill(
            db_session,
            sample_category.id,
            name="School",
            paid_from="sinking_fund",
            paid_from_fund_id=other.id,
            next_due_date="2026-06-01",
            contract_id=old.id,
        )
        fund_txn = confirm_payment(
            db_session, fund_bill, Decimal("15.00"), "2026-06-01"
        )
        db_session.commit()
        assert fund_txn.transaction.transaction_type == "regular"
        assert fund_txn.transaction.contract_id == old.id

        auto_bill = _bill(
            db_session,
            sample_category.id,
            name="Rates",
            next_due_date="2026-06-01",
            contract_id=old.id,
        )
        process_due_bills(db=db_session)
        auto_txn = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == auto_bill.id)
            .one()
        )
        assert auto_txn.transaction_type == "regular"
        assert auto_txn.contract_id == old.id
        db_session.refresh(auto_bill)
        assert auto_bill.is_active is True
        assert auto_bill.next_due_date == "2026-07-01"

        pay_bill = _bill(
            db_session,
            sample_category.id,
            name="Water",
            next_due_date="2026-08-01",
            contract_id=old.id,
        )
        paid = pay_bills_fund(
            db_session, pay_bill, Decimal("20.00"), "2026-08-01"
        ).transaction
        db_session.commit()
        assert paid.transaction_type == "regular"
        assert paid.contract_id == old.id

        budget_bill.contract_id = new.id
        fund_bill.contract_id = new.id
        auto_bill.contract_id = new.id
        pay_bill.contract_id = None
        db_session.commit()
        db_session.refresh(confirmed.transaction)
        db_session.refresh(fund_txn.transaction)
        db_session.refresh(auto_txn)
        db_session.refresh(paid)
        assert confirmed.transaction.contract_id == old.id
        assert fund_txn.transaction.contract_id == old.id
        assert auto_txn.contract_id == old.id
        assert paid.contract_id == old.id

    def test_ended_contract_still_auto_pays(
        self, db_session, sample_category, monkeypatch
    ):
        monkeypatch.setattr("app.tasks._today", lambda: date(2026, 6, 1))
        db_session.add(
            SinkingFund(
                name="Bills", color="#ff0000", current_balance=200, is_system=True
            )
        )
        contract = Contract(
            name="Power",
            provider="Origin",
            service="electricity",
            bill_delivery="post",
            ended_on="2026-05-01",
        )
        db_session.add(contract)
        db_session.commit()
        bill = _bill(
            db_session,
            sample_category.id,
            next_due_date="2026-06-01",
            contract_id=contract.id,
        )
        process_due_bills(db=db_session)
        txn = (
            db_session.query(Transaction)
            .filter(Transaction.recurring_bill_id == bill.id)
            .one()
        )
        assert txn.contract_id == contract.id
        db_session.refresh(bill)
        assert bill.is_active is True
        assert bill.next_due_date == "2026-07-01"


def test_brisbane_today_follows_the_configured_zone(monkeypatch):
    from app.services import contracts as contracts_service

    monkeypatch.undo()
    assert contracts_service.brisbane_today() == datetime.now(TIMEZONE).date()


def test_docs_name_contracts_and_keep_mcp_out_of_scope():
    root = Path(__file__).resolve().parents[1]
    agents = (root / "AGENTS.md").read_text()
    readme = (root / "README.md").read_text()
    assert "/api/contracts" in agents
    assert "No contract tools in v1" in agents
    assert "no `contracts` subcommand" in agents
    assert "58 API endpoints" in agents
    assert "**Contracts**" in readme
    assert "no contract tools in v1" in readme
    assert "no `glow contracts` command" in readme
