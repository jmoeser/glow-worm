"""Portfolio value series used by the shares chart."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

from app.models import FxRate, SharePrice
from app.services.shares import (
    TradeIn,
    build_portfolio_series,
    filter_portfolio_series,
    portfolio_chart_json,
)


def _trade(**overrides) -> TradeIn:
    values = {
        "id": 1,
        "ticker": "AAPL",
        "trade_type": "buy",
        "trade_date": date(2025, 3, 10),
        "quantity": Decimal(10),
        "price": Decimal(100),
        "currency": "USD",
        "fee": Decimal(0),
        "fee_currency": "USD",
        "fx_aud_usd": Decimal("0.50"),
    }
    values.update(overrides)
    return TradeIn(**values)


def _series(**kwargs):
    trades = kwargs.pop("trades", {"AAPL": [_trade()]})
    prices = kwargs.pop(
        "prices",
        {"AAPL": [(date(2025, 3, 10), Decimal(100))]},
    )
    fx = kwargs.pop("fx", [(date(2025, 3, 10), Decimal("0.50"))])
    return build_portfolio_series(trades, prices, fx, **kwargs)


class TestBuildPortfolioSeries:
    def test_empty_history(self):
        assert build_portfolio_series({}, {}, []) == []

    def test_buy_day_value_matches_cost_when_close_equals_trade_price(self):
        points = _series()
        assert len(points) == 1
        assert points[0].day == date(2025, 3, 10)
        # 10 * 100 USD / 0.50 = 2,000.00 AUD, fee 0.
        assert points[0].value_aud == Decimal("2000.00")
        assert points[0].cost_aud == Decimal("2000.00")

    def test_carries_close_and_fx_across_gaps(self):
        points = _series(
            prices={
                "AAPL": [
                    (date(2025, 3, 12), Decimal(90)),
                    (date(2025, 3, 10), Decimal(100)),
                ]
            },
            fx=[
                (date(2025, 3, 12), Decimal("0.70")),
                (date(2025, 3, 11), Decimal("0.70")),
                (date(2025, 3, 10), Decimal("0.65")),
            ],
            trades={"AAPL": [_trade(fx_aud_usd=Decimal("0.65"))]},
        )
        by_day = {point.day: point for point in points}
        # 10 * 100 / 0.65
        assert by_day[date(2025, 3, 10)].value_aud == Decimal("1538.46")
        assert by_day[date(2025, 3, 10)].cost_aud == Decimal("1538.46")
        # No close on 11 March: the 10 March close is reused at the new rate.
        assert by_day[date(2025, 3, 11)].value_aud == Decimal("1428.57")
        assert by_day[date(2025, 3, 11)].cost_aud == Decimal("1538.46")
        # 12 March uses the new close and the carried 0.70 rate. 900 / 0.70.
        assert by_day[date(2025, 3, 12)].value_aud == Decimal("1285.71")
        assert by_day[date(2025, 3, 12)].cost_aud == Decimal("1538.46")

    def test_same_day_price_uses_the_later_observation(self):
        points = _series(
            prices={
                "AAPL": [
                    (date(2025, 3, 10), Decimal(100)),
                    (date(2025, 3, 10), Decimal(110)),
                    (date(2025, 3, 11), Decimal(110)),
                ]
            }
        )
        assert points[0].value_aud == Decimal("2200.00")

    def test_skips_days_until_every_open_holding_has_a_price(self):
        trades = {
            "AAPL": [_trade()],
            "MSFT": [
                _trade(
                    id=2,
                    ticker="MSFT",
                    quantity=Decimal(2),
                    price=Decimal(50),
                )
            ],
        }
        points = build_portfolio_series(
            trades,
            {
                "AAPL": [
                    (date(2025, 3, 10), Decimal(100)),
                    (date(2025, 3, 11), Decimal(100)),
                ],
                "MSFT": [(date(2025, 3, 11), Decimal(50))],
            },
            [(date(2025, 3, 10), Decimal("0.50"))],
        )
        assert [point.day for point in points] == [date(2025, 3, 11)]
        assert points[0].value_aud == Decimal("2200.00")
        assert points[0].cost_aud == Decimal("2200.00")

    def test_aud_holding_does_not_need_fx(self):
        points = build_portfolio_series(
            {
                "BHP.AX": [
                    _trade(
                        ticker="BHP.AX",
                        currency="AUD",
                        price=Decimal(40),
                        fee_currency="AUD",
                        fx_aud_usd=None,
                    )
                ]
            },
            {
                "BHP.AX": [
                    (date(2025, 3, 10), Decimal(40)),
                    (date(2025, 3, 11), Decimal(42)),
                ]
            },
            [],
        )
        assert [point.value_aud for point in points] == [
            Decimal("400.00"),
            Decimal("420.00"),
        ]
        assert points[0].cost_aud == Decimal("400.00")

    def test_mixed_book_waits_for_fx(self):
        points = build_portfolio_series(
            {
                "BHP.AX": [
                    _trade(
                        ticker="BHP.AX",
                        currency="AUD",
                        price=Decimal(40),
                        fee_currency="AUD",
                        fx_aud_usd=None,
                    )
                ],
                "AAPL": [_trade(id=2)],
            },
            {
                "BHP.AX": [(date(2025, 3, 10), Decimal(40))],
                "AAPL": [(date(2025, 3, 10), Decimal(100))],
            },
            [(date(2025, 3, 11), Decimal("0.50"))],
        )
        assert [point.day for point in points] == [date(2025, 3, 11)]
        assert points[0].value_aud == Decimal("2400.00")

    def test_partial_sale_reduces_cost_base(self):
        points = _series(
            trades={
                "AAPL": [
                    _trade(),
                    _trade(
                        id=2,
                        trade_type="sell",
                        trade_date=date(2025, 3, 12),
                        quantity=Decimal(4),
                        price=Decimal(120),
                    ),
                ]
            },
            prices={
                "AAPL": [
                    (date(2025, 3, 10), Decimal(100)),
                    (date(2025, 3, 12), Decimal(120)),
                ]
            },
        )
        sold = points[-1]
        assert sold.day == date(2025, 3, 12)
        assert sold.cost_aud == Decimal("1200.00")
        assert sold.value_aud == Decimal("1440.00")

    def test_full_sale_marks_zero_once(self):
        points = _series(
            trades={
                "AAPL": [
                    _trade(),
                    _trade(
                        id=2,
                        trade_type="sell",
                        trade_date=date(2025, 3, 12),
                        quantity=Decimal(10),
                        price=Decimal(120),
                    ),
                ]
            },
            prices={
                "AAPL": [
                    (date(2025, 3, 10), Decimal(100)),
                    (date(2025, 3, 13), Decimal(130)),
                ]
            },
            fx=[
                (date(2025, 3, 10), Decimal("0.50")),
                (date(2025, 3, 13), Decimal("0.40")),
            ],
        )
        assert points[-1].day == date(2025, 3, 12)
        assert points[-1].value_aud == Decimal(0)
        assert points[-1].cost_aud == Decimal(0)
        assert date(2025, 3, 13) not in {point.day for point in points}

    def test_ignores_observations_after_as_of(self):
        points = _series(
            prices={
                "AAPL": [
                    (date(2025, 3, 10), Decimal(100)),
                    (date(2025, 4, 1), Decimal(200)),
                ]
            },
            as_of=date(2025, 3, 15),
        )
        assert [point.day for point in points] == [date(2025, 3, 10)]

    def test_fee_is_in_the_cost_base(self):
        points = _series(
            trades={
                "AAPL": [
                    _trade(fee=Decimal(3), fee_currency="USD"),
                ]
            },
            prices={
                "AAPL": [
                    (date(2025, 3, 10), Decimal(100)),
                    (date(2025, 3, 11), Decimal(100)),
                ]
            },
        )
        # (1000 + 3) / 0.50 = 2006.00
        assert points[0].cost_aud == Decimal("2006.00")
        assert points[0].value_aud == Decimal("2000.00")


class TestFilterPortfolioSeries:
    def _points(self) -> list:
        return build_portfolio_series(
            {"AAPL": [_trade(trade_date=date(2026, 1, 1), fx_aud_usd=Decimal("0.50"))]},
            {
                "AAPL": [
                    (date(2026, 1, 1), Decimal(100)),
                    (date(2026, 2, 15), Decimal(110)),
                    (date(2026, 3, 1), Decimal(120)),
                ]
            },
            [(date(2026, 1, 1), Decimal("0.50"))],
        )

    def test_one_month_keeps_the_boundary_day(self):
        kept = filter_portfolio_series(
            self._points(), as_of=date(2026, 3, 10), range_label="1M"
        )
        assert [point.day for point in kept] == [
            date(2026, 2, 15),
            date(2026, 3, 1),
        ]

    def test_all_and_unknown_labels_keep_every_point(self):
        points = self._points()
        assert (
            filter_portfolio_series(points, as_of=date(2026, 3, 10), range_label="All")
            == points
        )
        assert (
            filter_portfolio_series(points, as_of=date(2026, 3, 10), range_label="2Y")
            == points
        )

    def test_one_year_uses_calendar_months(self):
        kept = filter_portfolio_series(
            self._points(), as_of=date(2026, 3, 10), range_label="1Y"
        )
        assert [point.day for point in kept] == [
            date(2026, 1, 1),
            date(2026, 2, 15),
            date(2026, 3, 1),
        ]


class TestChartPayload:
    def test_json_uses_cent_strings(self):
        points = _series()
        payload = json.loads(portfolio_chart_json(points, today=date(2026, 3, 10)))
        assert payload["today"] == "2026-03-10"
        assert payload["points"][0]["value_aud"] == "2000.00"
        assert payload["points"][0]["cost_aud"] == "2000.00"


class TestSharesChartPage:
    def test_empty_page_explains_the_missing_chart(self, authed_client):
        response = authed_client.get("/shares")
        assert response.status_code == 200
        assert "Portfolio value" in response.text
        assert "Not enough price history yet" in response.text
        assert "d3.v7.min.js" in response.text

    def test_two_valued_days_are_embedded(self, authed_client, db_session):
        client = authed_client
        client.post(
            "/shares/trades",
            data={
                "ticker": "AAPL",
                "trade_type": "buy",
                "trade_date": "2025-03-10",
                "quantity": "10",
                "price": "100",
                "fee": "0",
                "fee_currency": "USD",
                "fx_rate": "0.65",
                "fx_input_convention": "AUD/USD",
            },
            headers={"x-csrftoken": client.csrf_token},
        )
        for day, close, rate in (
            ("2025-03-10", Decimal(100), Decimal("0.65")),
            ("2025-03-11", Decimal(110), Decimal("0.65")),
        ):
            db_session.add(
                SharePrice(
                    ticker="AAPL",
                    price_date=day,
                    close=close,
                    currency="USD",
                    source="alphavantage",
                    fetched_at=datetime.now(UTC),
                )
            )
            db_session.add(
                FxRate(
                    base="AUD",
                    quote="USD",
                    rate_date=day,
                    rate=rate,
                    source="frankfurter",
                    fetched_at=datetime.now(UTC),
                )
            )
        db_session.commit()
        response = client.get("/shares")
        assert "shares-chart-data" in response.text
        assert 'data-range="1Y"' in response.text
        assert '"value_aud":"1692.31"' in response.text
        assert '"cost_aud":"1538.46"' in response.text
