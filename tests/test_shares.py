"""Shares page and read-only API tests."""

import json
from datetime import UTC, datetime
from decimal import Decimal

from app.auth import generate_api_key, hash_api_key
from app.models import ApiKey, FxRate, SharePrice, ShareTrade


def _headers(client):
    return {"x-csrftoken": client.csrf_token}


def _buy(client, **overrides):
    data = {
        "ticker": "AAPL",
        "trade_type": "buy",
        "trade_date": "2025-03-10",
        "quantity": "10",
        "price": "100",
        "fee": "0",
        "fee_currency": "USD",
        "fx_rate": "0.65",
        "fx_input_convention": "AUD/USD",
    }
    data.update(overrides)
    return client.post("/shares/trades", data=data, headers=_headers(client))


class TestSharesPage:
    def test_empty_state(self, authed_client):
        response = authed_client.get("/shares")
        assert response.status_code == 200
        assert "Shares" in response.text
        assert "No open holdings" in response.text
        assert "Add trade" in response.text

    def test_no_key_banner(self, authed_client, monkeypatch):
        monkeypatch.setattr("app.config.ALPHA_VANTAGE_API_KEY", None)
        response = authed_client.get("/shares")
        assert "ALPHA_VANTAGE_API_KEY" in response.text
        assert "Daily price updates are off" in response.text

    def test_unauthenticated_redirects(self, client):
        response = client.get("/shares", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_nav_contains_shares_link(self, authed_client):
        response = authed_client.get("/")
        assert 'href="/shares"' in response.text


class TestTradeMutations:
    def test_buy_normalises_usd_aud_rate(self, authed_client, db_session):
        response = _buy(
            authed_client,
            ticker="XYZ",
            trade_date="2025-09-15",
            quantity="5",
            price="120",
            fee="3",
            fx_rate="1.5",
            fx_input_convention="USD/AUD",
        )
        assert response.status_code == 200
        assert response.headers["HX-Trigger-After-Swap"] == "gwShareTradeSuccess"
        assert "XYZ" in response.text
        trade = db_session.query(ShareTrade).filter(ShareTrade.ticker == "XYZ").one()
        assert trade.fx_rate_aud_usd == Decimal("0.6666666667")
        assert trade.fx_rate_input == Decimal(
            "1.5000000000"
        ) or trade.fx_rate_input == Decimal("1.5")
        assert trade.fx_input_convention == "USD/AUD"

    def test_oversell_returns_error_and_saves_nothing(self, authed_client, db_session):
        response = _buy(
            authed_client,
            trade_type="sell",
            quantity="1",
        )
        assert "exceeds holdings" in response.text
        assert db_session.query(ShareTrade).count() == 0

    def test_ax_without_fx(self, authed_client, db_session):
        response = _buy(
            authed_client,
            ticker="BHP.AX",
            price="40",
            fee="0",
            fee_currency="USD",
            fx_rate="",
            fx_input_convention="",
        )
        assert response.status_code == 200
        trade = db_session.query(ShareTrade).one()
        assert trade.currency == "AUD"
        assert trade.fx_rate_aud_usd is None
        assert trade.fee_currency == "AUD"
        assert "BHP.AX" in response.text

    def test_unsupported_suffix(self, authed_client, db_session):
        response = _buy(authed_client, ticker="TSCO.LON")
        assert "ASX" in response.text
        assert db_session.query(ShareTrade).count() == 0

    def test_missing_fx_for_us_ticker(self, authed_client, db_session):
        response = _buy(authed_client, fx_rate="")
        assert "AUD/USD" in response.text
        assert db_session.query(ShareTrade).count() == 0

    def test_implausible_fx_hint(self, authed_client):
        response = _buy(authed_client, fx_rate="1.5385", fx_input_convention="AUD/USD")
        assert "USD/AUD" in response.text

    def test_edit_trade(self, authed_client, db_session):
        _buy(authed_client, ticker="AAPL", quantity="4")
        trade = db_session.query(ShareTrade).one()
        edit = authed_client.get(f"/shares/trades/{trade.id}/edit")
        assert edit.status_code == 200
        assert 'name="quantity"' in edit.text
        response = authed_client.post(
            f"/shares/trades/{trade.id}",
            data={
                "ticker": "AAPL",
                "trade_type": "buy",
                "trade_date": "2025-03-10",
                "quantity": "6",
                "price": "100",
                "fee": "0",
                "fee_currency": "USD",
                "fx_rate": "0.65",
                "fx_input_convention": "AUD/USD",
            },
            headers=_headers(authed_client),
        )
        assert response.status_code == 200
        db_session.refresh(trade)
        assert trade.quantity == Decimal(6)

    def test_delete_trade(self, authed_client, db_session):
        _buy(authed_client)
        trade = db_session.query(ShareTrade).one()
        response = authed_client.delete(
            f"/shares/trades/{trade.id}",
            headers=_headers(authed_client),
        )
        assert response.status_code == 200
        assert db_session.query(ShareTrade).count() == 0

    def test_delete_buy_that_a_sell_depends_on(self, authed_client, db_session):
        _buy(authed_client, quantity="10", trade_date="2025-03-10")
        _buy(
            authed_client,
            trade_type="sell",
            quantity="4",
            trade_date="2026-03-02",
            price="150",
            fx_rate="0.70",
        )
        buy = db_session.query(ShareTrade).filter(ShareTrade.trade_type == "buy").one()
        response = authed_client.delete(
            f"/shares/trades/{buy.id}",
            headers=_headers(authed_client),
        )
        assert response.status_code == 400
        assert "Can't delete" in response.text
        assert "2026-03-02" in response.text
        assert db_session.query(ShareTrade).count() == 2

    def test_fx_preview(self, authed_client):
        response = authed_client.get(
            "/shares/fx-preview",
            params={
                "ticker": "XYZ",
                "trade_type": "buy",
                "quantity": "5",
                "price": "120",
                "fee": "3",
                "fee_currency": "USD",
                "fx_rate": "1.5",
                "fx_input_convention": "USD/AUD",
            },
        )
        assert response.status_code == 200
        assert "0.6666666667" in response.text
        assert "904.50" in response.text


class TestShareDetail:
    def test_shows_parcels_and_realised(self, authed_client, db_session):
        _buy(
            authed_client,
            ticker="XYZ",
            trade_date="2025-03-10",
            quantity="10",
            price="100",
            fee="3",
            fx_rate="0.63",
        )
        _buy(
            authed_client,
            ticker="XYZ",
            trade_date="2025-09-15",
            quantity="5",
            price="120",
            fee="3",
            fx_rate="1.5",
            fx_input_convention="USD/AUD",
        )
        _buy(
            authed_client,
            ticker="XYZ",
            trade_type="sell",
            trade_date="2026-03-02",
            quantity="12",
            price="150",
            fee="3",
            fx_rate="0.70",
        )
        db_session.add(
            SharePrice(
                ticker="XYZ",
                price_date="2026-10-16",
                close=Decimal(140),
                currency="USD",
                source="alphavantage",
                fetched_at=datetime.now(UTC),
            )
        )
        db_session.add(
            FxRate(
                base="AUD",
                quote="USD",
                rate_date="2026-10-16",
                rate=Decimal("0.6500"),
                source="frankfurter",
                fetched_at=datetime.now(UTC),
            )
        )
        db_session.commit()
        response = authed_client.get("/shares/XYZ")
        assert response.status_code == 200
        assert "2025-09-15" in response.text
        assert "542.70" in response.text or "103.45" in response.text
        assert "2026-03-02" in response.text

    def test_unknown_ticker_is_404(self, authed_client):
        response = authed_client.get("/shares/NOPE")
        assert response.status_code == 404


class TestSharesApi:
    def test_json_uses_floats(self, authed_client):
        _buy(authed_client, ticker="AAPL", quantity="2", price="50", fee="0")
        response = authed_client.get("/api/shares")
        assert response.status_code == 200
        payload = json.loads(response.text)
        assert isinstance(
            payload["totals"]["cost_aud"]
            if "totals" in payload
            else payload["cost_aud"],
            float,
        )
        holding = payload["holdings"][0]
        assert isinstance(holding["cost_aud"], float)
        assert "realised_by_fy" in payload

    def test_bearer_token(self, client, db_session, test_user):
        plain = generate_api_key()
        db_session.add(
            ApiKey(
                user_id=test_user.id,
                key_hash=hash_api_key(plain),
                name="shares",
            )
        )
        db_session.commit()
        response = client.get(
            "/api/shares",
            headers={"Authorization": f"Bearer {plain}"},
        )
        assert response.status_code == 200
        assert "holdings" in response.json()
