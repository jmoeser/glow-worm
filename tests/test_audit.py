"""Audit log for successful REST and MCP writes."""

import asyncio
import io
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from unittest.mock import patch

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.audit import (
    REDACTED,
    AuditStreamHandler,
    audit_write,
    column_changes,
    configure_audit_logging,
    emit_audit,
    serialize_audit_value,
    summary_for,
)
from app.auth import generate_api_key, hash_api_key
from app.main import app
from app.mcp_server import mcp
from app.middleware import AuthenticationMiddleware
from app.models import ApiKey, Budget, Category, RecurringBill, SinkingFund

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

REST_WRITES = {
    ("POST", "/api/keys"),
    ("DELETE", "/api/keys/{key_id}"),
    ("POST", "/api/bills"),
    ("PUT", "/api/bills/{bill_id}"),
    ("DELETE", "/api/bills/{bill_id}"),
    ("POST", "/api/bills/{bill_id}/pay"),
    ("POST", "/api/bills/{bill_id}/confirm"),
    ("POST", "/api/bills/{bill_id}/skip"),
    ("POST", "/api/budgets"),
    ("PUT", "/api/budgets/{budget_id}"),
    ("DELETE", "/api/budgets/{budget_id}"),
    ("POST", "/api/categories"),
    ("PUT", "/api/categories/{cat_id}"),
    ("DELETE", "/api/categories/{cat_id}"),
    ("POST", "/api/contracts"),
    ("PUT", "/api/contracts/{contract_id}"),
    ("POST", "/api/contracts/{contract_id}/end"),
    ("DELETE", "/api/contracts/{contract_id}"),
    ("POST", "/api/income"),
    ("POST", "/api/income/secondary"),
    ("POST", "/api/income/secondary/record"),
    ("POST", "/api/payment-methods"),
    ("PUT", "/api/payment-methods/{method_id}"),
    ("DELETE", "/api/payment-methods/{method_id}"),
    ("POST", "/api/payment-methods/{method_id}/archive"),
    ("POST", "/api/payment-methods/{method_id}/unarchive"),
    ("POST", "/api/sinking-funds"),
    ("PUT", "/api/sinking-funds/{fund_id}"),
    ("DELETE", "/api/sinking-funds/{fund_id}"),
    ("POST", "/api/transactions"),
    ("PUT", "/api/transactions/{txn_id}"),
    ("DELETE", "/api/transactions/{txn_id}"),
    ("POST", "/api/users"),
    ("PUT", "/api/users/{user_id}"),
    ("DELETE", "/api/users/{user_id}"),
}

MCP_WRITES = {
    "create_transaction",
    "update_transaction",
    "delete_transaction",
    "create_bill",
    "update_bill",
    "delete_bill",
    "confirm_payment",
    "skip_payment",
    "create_payment_method",
    "update_payment_method",
    "archive_payment_method",
}


class _Sample(Enum):
    FIXED = "fixed"


@pytest.fixture
def audit_events():
    events: list[dict] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            events.append(json.loads(record.getMessage()))

    handler = _ListHandler()
    logger = logging.getLogger("glow_worm.audit")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield events
    finally:
        logger.removeHandler(handler)


def _iter_routes(routes):
    """Yield concrete routes. FastAPI 0.162 stores included routers as
    ``_IncludedRouter`` instead of flattening them onto ``app.routes``.
    """
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _iter_routes(nested.routes)
            continue
        yield route


def _api_writes():
    from starlette.routing import Mount

    found = []
    for route in _iter_routes(app.routes):
        if isinstance(route, Mount):
            continue
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", None) or ())
        if path.startswith("/api/") and methods & WRITE_METHODS:
            found.append(route)
    return found


class TestSerializing:
    def test_decimal_numeric_none_dates_and_enums(self, db_session, sample_category):
        assert serialize_audit_value(None) is None
        assert serialize_audit_value(Decimal("12.5")) == "12.50"
        assert serialize_audit_value(12.5) == "12.50"
        assert serialize_audit_value("2026-11-01") == "2026-11-01"
        assert serialize_audit_value(date(2026, 11, 1)) == "2026-11-01"
        aware = datetime(2026, 11, 11, 1, 44, 0, 123456, tzinfo=UTC)
        assert serialize_audit_value(aware) == "2026-11-11T01:44:00.123456Z"
        naive = datetime(2026, 11, 11, 1, 44, 0, 123456)
        assert serialize_audit_value(naive) == "2026-11-11T01:44:00.123456Z"
        brisbane = datetime(
            2026,
            11,
            11,
            11,
            44,
            0,
            123456,
            tzinfo=timezone(timedelta(hours=10)),
        )
        assert serialize_audit_value(brisbane) == "2026-11-11T01:44:00.123456Z"
        assert serialize_audit_value(_Sample.FIXED) == "fixed"
        # A datetime must not be formatted as a plain date. datetime is a date subclass.
        assert "T" in serialize_audit_value(aware)

        budget = Budget(
            category_id=sample_category.id,
            month=11,
            year=2026,
            allocated_amount=Decimal("12.5"),
        )
        db_session.add(budget)
        db_session.commit()
        db_session.refresh(budget)
        assert isinstance(budget.allocated_amount, Decimal)
        assert serialize_audit_value(budget.allocated_amount) == "12.50"

    def test_redacts_secrets_and_records_password_changes(self):
        changes = column_changes(
            {
                "password_hash": "old-hash",  # pragma: allowlist secret
                "email": "old@example.com",
                "last4": "1111",
                "key_hash": "hash-1",
                "key": "gw_secret",
                "name": "Old",
                "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
            {
                "password_hash": "new-hash",  # pragma: allowlist secret
                "email": "new@example.com",
                "last4": "2222",
                "key_hash": "hash-2",
                "key": "gw_other",
                "name": "New",
                "updated_at": datetime(2026, 2, 1, tzinfo=UTC),
            },
        )
        assert changes["password"] == {"old": REDACTED, "new": REDACTED}
        assert changes["name"] == {"old": "Old", "new": "New"}
        assert "password_hash" not in changes
        assert "email" not in changes
        assert "last4" not in changes
        assert "key_hash" not in changes
        assert "key" not in changes
        assert "updated_at" not in changes
        rendered = json.dumps(changes)
        for secret in (
            "old-hash",
            "new-hash",
            "old@example.com",
            "new@example.com",
            "1111",
            "2222",
            "hash-1",
            "gw_secret",
        ):
            assert secret not in rendered

        assert summary_for(
            "api_key",
            {"id": 3, "name": "laptop", "user_id": 1, "key_hash": "abc", "key": "raw"},
        ) == {"id": 3, "name": "laptop", "user_id": 1}
        assert "last4" not in summary_for(
            "payment_method",
            {
                "id": 1,
                "nickname": "Visa",
                "kind": "card",
                "owner": "Jordan",
                "last4": "4321",
            },
        )
        assert summary_for(
            "user",
            {
                "id": 2,
                "username": "bob",
                "email": "bob@example.com",
                "password_hash": "x",
            },
        ) == {"id": 2, "username": "bob"}


class TestHandler:
    def test_stdout_handler_ignores_root_level(self):
        configure_audit_logging()
        logger = logging.getLogger("glow_worm.audit")
        assert logger.propagate is False
        handlers = [h for h in logger.handlers if isinstance(h, AuditStreamHandler)]
        assert len(handlers) == 1
        handler = handlers[0]
        assert handler.stream is sys.stdout
        assert handler.formatter is not None
        assert handler.formatter._fmt == "%(message)s"

        root = logging.getLogger()
        previous = root.level
        buffer = io.StringIO()
        handler.stream = buffer
        root.setLevel(logging.CRITICAL)
        try:
            emit_audit(
                source="rest",
                action="update",
                entity_type="category",
                entity_id=1,
                endpoint="/api/categories/1",
                changes={"name": {"old": "A", "new": "B"}},
                actor=None,
            )
            line = buffer.getvalue().strip()
            payload = json.loads(line)
            assert payload["event"] == "glow_worm.audit"
            assert not line.startswith("INFO")
        finally:
            handler.stream = sys.stdout
            root.setLevel(previous)

    def test_marker_does_not_emit(self, audit_events):
        @audit_write
        def sample():
            return "ok"

        assert sample() == "ok"
        assert sample.__audit_covered__ is True
        assert audit_events == []

    def test_serialize_and_log_failures_are_swallowed(self, caplog):
        with (
            patch(
                "app.audit.serialize_audit_value",
                side_effect=RuntimeError("bad value"),
            ),
            caplog.at_level(logging.ERROR, logger="app"),
        ):
            emit_audit(
                source="rest",
                action="create",
                entity_type="category",
                entity_id=1,
                endpoint="/api/categories",
                values={"id": 1, "name": "Food"},
            )
        assert "audit log failed" in caplog.text


class TestRestWrites:
    def test_create_update_and_delete_shapes(
        self, authed_client, audit_events, test_user
    ):
        created = authed_client.post(
            "/api/transactions",
            json={
                "date": "2026-11-01",
                "description": "Coffee",
                "amount": "12.5",
                "category_id": _expense_category(authed_client),
                "type": "expense",
            },
        )
        assert created.status_code == 201
        body = created.json()
        create_event = audit_events[-1]
        assert create_event["event"] == "glow_worm.audit"
        assert create_event["source"] == "rest"
        assert create_event["action"] == "create"
        assert create_event["entity_type"] == "transaction"
        assert create_event["entity_id"] == body["id"]
        assert create_event["user"] == "alice"
        assert create_event["user_id"] == test_user.id
        assert create_event["auth_method"] == "session"
        assert create_event["api_key_id"] is None
        assert create_event["api_key_name"] is None
        assert create_event["endpoint"] == "/api/transactions"
        assert create_event["values"] == {
            "id": body["id"],
            "date": "2026-11-01",
            "amount": "12.50",
            "description": "Coffee",
            "type": "expense",
        }
        assert "changes" not in create_event
        assert "category_id" not in create_event["values"]

        audit_events.clear()
        updated = authed_client.put(
            f"/api/sinking-funds/{_fund(authed_client)}",
            json={"name": "Holiday fund"},
        )
        assert updated.status_code == 200
        update_event = audit_events[-1]
        assert update_event["action"] == "update"
        assert update_event["changes"] == {
            "name": {"old": "Holiday", "new": "Holiday fund"}
        }
        assert "updated_at" not in update_event["changes"]
        assert "current_balance" not in update_event["changes"]
        assert "values" not in update_event

        audit_events.clear()
        deleted = authed_client.delete(f"/api/transactions/{body['id']}")
        assert deleted.status_code == 200
        delete_event = audit_events[-1]
        assert delete_event["action"] == "delete"
        assert delete_event["entity_type"] == "transaction"
        assert delete_event["entity_id"] == body["id"]
        assert delete_event["values"]["description"] == "Coffee"
        assert "changes" not in delete_event

    def test_confirm_uses_pre_transition_cursor(
        self, authed_client, db_session, audit_events
    ):
        category = Category(name="Bills", type="expense", color="#111111")
        fund = SinkingFund(
            name="Bills", color="#ff0000", current_balance=500, is_system=True
        )
        db_session.add_all([category, fund])
        db_session.flush()
        bill = RecurringBill(
            name="Water",
            amount=40,
            debtor_provider="Council",
            start_date="2026-01-01",
            frequency="monthly",
            category_id=category.id,
            next_due_date="2026-11-01",
            paid_from="bills_fund",
            bill_type="variable",
        )
        db_session.add(bill)
        db_session.commit()
        audit_events.clear()

        response = authed_client.post(
            f"/api/bills/{bill.id}/confirm",
            json={"amount": "40.00", "due_date": "2026-11-01"},
        )
        assert response.status_code == 200, response.text
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["action"] == "confirm"
        assert event["entity_type"] == "bill"
        assert event["entity_id"] == bill.id
        assert event["source"] == "rest"
        assert event["changes"]["next_due_date"] == {
            "old": "2026-11-01",
            "new": "2026-12-01",
        }
        assert event["related"] == [
            {
                "entity_type": "transaction",
                "entity_id": response.json()["transaction"]["id"],
                "action": "create",
            }
        ]
        assert "current_balance" not in json.dumps(event)

    def test_password_and_api_key_material_stay_out_of_the_line(
        self, authed_client, audit_events
    ):
        created = authed_client.post(
            "/api/users",
            json={
                "username": "bob",
                "password": "SecretPass1",  # pragma: allowlist secret
                "email": "bob@example.com",
            },
        )
        assert created.status_code == 201
        create_line = json.dumps(audit_events[-1])
        assert "SecretPass1" not in create_line
        assert "bob@example.com" not in create_line
        assert audit_events[-1]["values"] == {
            "id": created.json()["id"],
            "username": "bob",
        }

        audit_events.clear()
        updated = authed_client.put(
            f"/api/users/{created.json()['id']}",
            json={
                "password": "NewSecret2",  # pragma: allowlist secret
                "email": "next@example.com",
            },
        )
        assert updated.status_code == 200
        event = audit_events[-1]
        assert event["changes"]["password"] == {"old": REDACTED, "new": REDACTED}
        line = json.dumps(event)
        assert "NewSecret2" not in line
        assert "next@example.com" not in line
        assert "password_hash" not in event["changes"]
        assert "email" not in event["changes"]

        audit_events.clear()
        key = authed_client.post("/api/keys", json={"name": "laptop-cli"})
        assert key.status_code == 201
        plain = key.json()["key"]
        key_event = audit_events[-1]
        key_line = json.dumps(key_event)
        assert plain not in key_line
        assert "key_hash" not in key_line
        assert set(key_event["values"]) == {"id", "name", "user_id"}
        assert key_event["values"]["name"] == "laptop-cli"

    def test_failures_and_reads_emit_nothing(self, authed_client, audit_events):
        invalid = authed_client.post("/api/categories", json={"name": ""})
        assert invalid.status_code == 422
        missing = authed_client.put("/api/bills/99999", json={"name": "Nope"})
        assert missing.status_code == 404

        created = authed_client.post(
            "/api/payment-methods",
            json={"nickname": "Cash", "kind": "manual", "owner": "Jordan"},
        )
        assert created.status_code == 201
        audit_events.clear()
        fetched = authed_client.get(f"/api/payment-methods/{created.json()['id']}")
        assert fetched.status_code == 200
        assert audit_events == []

    def test_audit_failure_does_not_fail_the_request(self, authed_client, caplog):
        with (
            patch("app.audit._write_audit_line", side_effect=RuntimeError("disk full")),
            caplog.at_level(logging.ERROR, logger="app"),
        ):
            response = authed_client.post(
                "/api/categories",
                json={"name": "Food", "type": "expense", "color": "#112233"},
            )
        assert response.status_code == 201
        assert "audit log failed" in caplog.text

    def test_bill_update_records_the_mark_and_the_note(
        self, authed_client, audit_events
    ):
        category_id = _expense_category(authed_client)
        created = authed_client.post(
            "/api/bills",
            json={
                "name": "Power",
                "amount": "40.00",
                "debtor_provider": "Grid",
                "start_date": "2026-01-01",
                "frequency": "monthly",
                "category_id": category_id,
                "next_due_date": "2026-06-01",
                "cancel_note": "Drafted first",
            },
        )
        assert created.status_code == 201, created.text
        create_event = audit_events[-1]
        assert create_event["action"] == "create"
        assert set(create_event["values"]) == {
            "id",
            "name",
            "amount",
            "next_due_date",
        }
        assert "Drafted first" not in json.dumps(create_event)
        audit_events.clear()

        updated = authed_client.put(
            f"/api/bills/{created.json()['id']}",
            json={
                "importance": "essential",
                "cancel_note": "Call the provider",
            },
        )
        assert updated.status_code == 200, updated.text
        event = audit_events[-1]
        assert event["action"] == "update"
        assert event["entity_type"] == "bill"
        assert event["changes"]["importance"] == {"old": None, "new": "essential"}
        assert event["changes"]["cancel_note"] == {
            "old": "Drafted first",
            "new": "Call the provider",
        }
        assert "updated_at" not in event["changes"]
        dumped = json.dumps(event)
        assert "[redacted]" not in dumped
        assert "Call the provider" in dumped


class TestMcpWrite:
    def test_frozen_actor_names_the_api_key(
        self, db_session, test_user, sample_category, audit_events
    ):
        plain = generate_api_key()
        db_session.add(
            ApiKey(
                user_id=test_user.id,
                key_hash=hash_api_key(plain),
                name="laptop-cli",
            )
        )
        db_session.commit()
        key_id = db_session.query(ApiKey).filter(ApiKey.name == "laptop-cli").one().id

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {plain}".encode())],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        request = Request(scope)
        seen: dict = {}

        async def call_next(_request):
            from app.audit import get_current_actor
            from app.mcp_server import create_transaction

            actor = get_current_actor()
            assert actor is not None
            assert actor.api_key_name == "laptop-cli"  # pragma: allowlist secret
            result = create_transaction(
                date="2026-11-02",
                amount=8.5,
                category_id=sample_category.id,
                type="expense",
                description="MCP coffee",
            )
            seen["result"] = result
            return JSONResponse({"ok": True})

        middleware = AuthenticationMiddleware(app=lambda *_args: None)
        response = asyncio.run(middleware.dispatch(request, call_next))
        assert response.status_code == 200
        assert isinstance(seen["result"], dict)
        assert len(audit_events) == 1
        event = audit_events[0]
        assert event["source"] == "mcp"
        assert event["endpoint"] == "create_transaction"
        assert event["action"] == "create"
        assert event["auth_method"] == "api_key"
        assert event["user"] == "alice"
        assert event["api_key_name"] == "laptop-cli"  # pragma: allowlist secret
        assert event["api_key_id"] == key_id
        assert event["values"]["amount"] == "8.50"
        assert "last4" not in json.dumps(event)


class TestCoverage:
    def test_every_api_write_is_marked(self):
        routes = _api_writes()
        actual = set()
        for route in routes:
            assert getattr(route.endpoint, "__audit_covered__", False) is True, (
                route.path
            )
            for method in route.methods or ():
                if method in WRITE_METHODS:
                    actual.add((method, route.path))
        assert actual == REST_WRITES

    def test_mcp_write_tools_are_marked_and_reads_are_not(self):
        from app import mcp_server

        tools = asyncio.run(mcp.list_tools())
        names = {tool.name for tool in tools}
        assert MCP_WRITES <= names
        assert "get_payment_method" in names
        for name in names:
            fn = getattr(mcp_server, name)
            marked = getattr(fn, "__audit_covered__", False) is True
            if name.startswith(("list_", "get_")):
                assert marked is False, name
            else:
                assert marked is True, name
        writes = {name for name in names if not name.startswith(("list_", "get_"))}
        assert writes == MCP_WRITES


def _expense_category(client) -> int:
    response = client.post(
        "/api/categories",
        json={"name": "Groceries", "type": "expense", "color": "#22C55E"},
    )
    assert response.status_code == 201
    return response.json()["id"]


def _fund(client) -> int:
    response = client.post(
        "/api/sinking-funds",
        json={"name": "Holiday", "color": "#112233", "current_balance": "10.00"},
    )
    assert response.status_code == 201
    return response.json()["id"]
