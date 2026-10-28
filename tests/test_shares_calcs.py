"""Pure FIFO, FX, and valuation tests. No database."""

from datetime import date
from decimal import Decimal

import pytest

from app.services.shares import (
    FxRateError,
    HoldingValuation,
    OpenParcel,
    OversellError,
    TradeIn,
    australian_financial_year,
    fee_in_trade_ccy,
    held_over_12_months,
    instrument_currency,
    match_fifo,
    normalise_fx,
    q2,
    summarise_portfolio,
    value_holding,
)

CENT = Decimal("0.01")


def _trade(
    trade_id: int,
    trade_type: str,
    day: str,
    quantity: str,
    price: str,
    *,
    fee: str = "0",
    fee_currency: str = "USD",
    fx: str | None = "0.65",
    currency: str = "USD",
    ticker: str = "XYZ",
) -> TradeIn:
    return TradeIn(
        id=trade_id,
        ticker=ticker,
        trade_type=trade_type,
        trade_date=date.fromisoformat(day),
        quantity=Decimal(quantity),
        price=Decimal(price),
        currency=currency,
        fee=Decimal(fee),
        fee_currency=fee_currency,
        fx_aud_usd=Decimal(fx) if fx is not None else None,
    )


def _worked_example() -> list[TradeIn]:
    return [
        _trade(1, "buy", "2025-03-10", "10", "100", fee="3", fx="0.6300"),
        _trade(
            2,
            "buy",
            "2025-09-15",
            "5",
            "120",
            fee="3",
            fx="0.6666666667",
        ),
        _trade(3, "sell", "2026-03-02", "12", "150", fee="3", fx="0.7000"),
    ]


class TestNormaliseFx:
    def test_aud_usd_passthrough(self):
        assert normalise_fx(Decimal("0.6300"), "AUD/USD") == Decimal("0.6300")

    def test_usd_aud_one_point_five(self):
        assert normalise_fx(Decimal("1.5"), "USD/AUD") == Decimal("0.6666666667")

    def test_usd_aud_one_point_five_three_eight_five(self):
        assert normalise_fx(Decimal("1.5385"), "USD/AUD") == Decimal("0.6499837504")

    @pytest.mark.parametrize(
        ("value", "convention"),
        [
            ("0.40", "AUD/USD"),
            ("1.25", "AUD/USD"),
            ("0.80", "USD/AUD"),
            ("2.50", "USD/AUD"),
        ],
    )
    def test_boundaries_accepted(self, value: str, convention: str):
        result = normalise_fx(Decimal(value), convention)
        assert result > 0

    @pytest.mark.parametrize(
        ("value", "convention"),
        [
            ("0.3999", "AUD/USD"),
            ("1.2501", "AUD/USD"),
            ("0.7999", "USD/AUD"),
            ("2.5001", "USD/AUD"),
        ],
    )
    def test_just_outside_rejected(self, value: str, convention: str):
        with pytest.raises(FxRateError):
            normalise_fx(Decimal(value), convention)

    @pytest.mark.parametrize("raw", ["0", "-1", "NaN", "Infinity", "-Infinity"])
    def test_non_positive_rejected(self, raw: str):
        with pytest.raises(FxRateError):
            normalise_fx(Decimal(raw), "AUD/USD")

    def test_swap_hint(self):
        with pytest.raises(FxRateError, match="USD/AUD"):
            normalise_fx(Decimal("1.5385"), "AUD/USD")


class TestInstrumentCurrency:
    def test_us_ticker(self):
        assert instrument_currency("AAPL") == "USD"

    def test_ax_suffix(self):
        assert instrument_currency("bhp.ax") == "AUD"

    def test_aus_suffix(self):
        assert instrument_currency("VAS.AUS") == "AUD"

    def test_other_suffix_rejected(self):
        with pytest.raises(ValueError):
            instrument_currency("TSCO.LON")


class TestFeeInTradeCcy:
    def test_usd_fee_on_usd_trade(self):
        assert fee_in_trade_ccy(Decimal(3), "USD", "USD", Decimal("0.65")) == Decimal(3)

    def test_aud_fee_on_usd_trade(self):
        assert fee_in_trade_ccy(Decimal(10), "AUD", "USD", Decimal("0.65")) == Decimal(
            "6.50"
        )

    def test_aud_fee_on_aud_trade(self):
        assert fee_in_trade_ccy(Decimal("9.95"), "AUD", "AUD", None) == Decimal("9.95")


class TestMatchFifo:
    def test_single_buy(self):
        result = match_fifo([_trade(1, "buy", "2025-01-01", "4", "10")])
        assert len(result.open_parcels) == 1
        assert result.open_parcels[0].remaining_qty == Decimal(4)
        assert result.realised == []

    def test_partial_sell_inside_one_parcel(self):
        result = match_fifo(
            [
                _trade(1, "buy", "2025-01-01", "10", "20", fee="0", fx="0.50"),
                _trade(2, "sell", "2025-06-01", "4", "25", fee="0", fx="0.50"),
            ]
        )
        assert len(result.realised) == 1
        assert result.realised[0].quantity == Decimal(4)
        assert result.open_parcels[0].remaining_qty == Decimal(6)

    def test_worked_example_figures(self):
        result = match_fifo(_worked_example())
        assert len(result.open_parcels) == 1
        parcel = result.open_parcels[0]
        assert parcel.buy_id == 2
        assert parcel.remaining_qty == Decimal(3)
        assert q2(parcel.cost_total_aud) == Decimal("904.50")
        assert q2(parcel.remaining_cost_aud) == Decimal("542.70")
        assert q2(parcel.remaining_cost_ccy) == Decimal("361.80")

        assert len(result.realised) == 2
        first, second = result.realised
        assert first.quantity == Decimal(10)
        assert q2(first.proceeds_ccy) == Decimal("1497.50")
        assert q2(first.proceeds_aud) == Decimal("2139.29")
        assert q2(first.cost_ccy) == Decimal("1003.00")
        assert q2(first.cost_aud) == Decimal("1592.06")
        assert q2(first.gain_aud) == Decimal("547.22")
        assert q2(first.gain_ccy) == Decimal("494.50")
        assert first.held_over_12_months is False
        assert first.financial_year == "FY2025-26"

        assert second.quantity == Decimal(2)
        assert q2(second.proceeds_ccy) == Decimal("299.50")
        assert q2(second.proceeds_aud) == Decimal("427.86")
        assert q2(second.cost_ccy) == Decimal("241.20")
        assert q2(second.cost_aud) == Decimal("361.80")
        assert q2(second.gain_aud) == Decimal("66.06")
        assert q2(second.gain_ccy) == Decimal("58.30")
        assert second.held_over_12_months is False
        assert second.financial_year == "FY2025-26"

        assert q2(first.gain_aud) + q2(second.gain_aud) == Decimal("613.28")
        assert q2(first.gain_ccy) + q2(second.gain_ccy) == Decimal("552.80")
        assert q2(first.proceeds_aud + second.proceeds_aud) == Decimal("2567.14")

    def test_sell_spanning_three_parcels(self):
        trades = [
            _trade(1, "buy", "2025-01-01", "1", "10", fee="0"),
            _trade(2, "buy", "2025-02-01", "1", "10", fee="0"),
            _trade(3, "buy", "2025-03-01", "1", "10", fee="0"),
            _trade(4, "sell", "2025-04-01", "2.5", "12", fee="0"),
        ]
        result = match_fifo(trades)
        quantities = [match.quantity for match in result.realised]
        assert quantities == [Decimal(1), Decimal(1), Decimal("0.5")]
        assert result.open_parcels[0].remaining_qty == Decimal("0.5")

    def test_two_sells_consume_one_parcel_costs_sum(self):
        trades = [
            _trade(1, "buy", "2025-01-01", "10", "100", fee="0", fx="0.50"),
            _trade(2, "sell", "2025-02-01", "4", "110", fee="0", fx="0.50"),
            _trade(3, "sell", "2025-03-01", "6", "120", fee="0", fx="0.50"),
        ]
        result = match_fifo(trades)
        assert result.open_parcels == []
        cost_aud = sum((match.cost_aud for match in result.realised), Decimal(0))
        cost_ccy = sum((match.cost_ccy for match in result.realised), Decimal(0))
        assert cost_ccy == Decimal(1000)
        assert cost_aud == Decimal(2000)

    def test_selling_out_leaves_no_open_parcels(self):
        result = match_fifo(
            [
                _trade(1, "buy", "2025-01-01", "2", "10"),
                _trade(2, "sell", "2025-02-01", "2", "11"),
            ]
        )
        assert result.open_parcels == []

    def test_same_day_buy_then_sell_is_ok(self):
        result = match_fifo(
            [
                _trade(1, "buy", "2025-05-01", "3", "10"),
                _trade(2, "sell", "2025-05-01", "3", "10"),
            ]
        )
        assert result.open_parcels == []
        assert len(result.realised) == 1

    def test_same_day_sell_before_buy_is_oversell(self):
        with pytest.raises(OversellError) as caught:
            match_fifo(
                [
                    _trade(1, "sell", "2025-05-01", "1", "10"),
                    _trade(2, "buy", "2025-05-01", "1", "10"),
                ]
            )
        assert caught.value.available == Decimal(0)

    def test_oversell_reports_available_quantity(self):
        with pytest.raises(OversellError) as caught:
            match_fifo(
                [
                    _trade(1, "buy", "2025-01-01", "2", "10"),
                    _trade(2, "sell", "2025-02-01", "5", "10"),
                ]
            )
        assert caught.value.available == Decimal(2)
        assert "2" in str(caught.value)

    def test_fractional_quantities(self):
        result = match_fifo(
            [
                _trade(1, "buy", "2025-01-01", "1.5", "10", fee="0"),
                _trade(2, "sell", "2025-02-01", "0.25", "12", fee="0"),
            ]
        )
        assert result.open_parcels[0].remaining_qty == Decimal("1.25")

    def test_aud_instrument_does_not_divide(self):
        result = match_fifo(
            [
                _trade(
                    1,
                    "buy",
                    "2025-01-01",
                    "10",
                    "40",
                    fee="1",
                    fee_currency="AUD",
                    fx=None,
                    currency="AUD",
                    ticker="BHP.AX",
                )
            ]
        )
        parcel = result.open_parcels[0]
        assert parcel.cost_total_aud == Decimal(401)
        assert parcel.cost_total_ccy == Decimal(401)


class TestHoldingPeriod:
    def test_anniversary_day_is_not_over_12_months(self):
        assert held_over_12_months(date(2025, 3, 10), date(2026, 3, 10)) is False

    def test_day_after_anniversary_is_over_12_months(self):
        assert held_over_12_months(date(2025, 3, 10), date(2026, 3, 11)) is True

    def test_leap_day_acquisition(self):
        assert held_over_12_months(date(2024, 2, 29), date(2025, 2, 28)) is False
        assert held_over_12_months(date(2024, 2, 29), date(2025, 3, 1)) is True


class TestFinancialYear:
    def test_end_of_june(self):
        assert australian_financial_year(date(2026, 6, 30)) == "FY2025-26"

    def test_first_of_july(self):
        assert australian_financial_year(date(2026, 7, 1)) == "FY2026-27"


class TestValuation:
    def test_worked_example_valuation(self):
        result = match_fifo(_worked_example())
        valued = value_holding(result, Decimal(140), Decimal("0.6500"))
        assert q2(valued.value_ccy) == Decimal("420.00")
        assert q2(valued.value_aud) == Decimal("646.15")
        assert q2(valued.unrealised_aud) == Decimal("103.45")
        assert q2(valued.unrealised_usd) == Decimal("58.20")
        assert valued.pct is not None
        assert q2(valued.pct) == Decimal("19.06")

    def test_unpriced_holding_excluded_from_totals(self):
        priced = value_holding(
            match_fifo([_trade(1, "buy", "2025-01-01", "1", "10", fee="0", fx="0.5")]),
            Decimal(12),
            Decimal("0.5"),
        )
        unpriced = value_holding(
            match_fifo(
                [
                    _trade(
                        1,
                        "buy",
                        "2025-01-01",
                        "1",
                        "10",
                        fee="0",
                        fx="0.5",
                        ticker="ZZ",
                    )
                ]
            ),
            None,
            Decimal("0.5"),
        )
        summary = summarise_portfolio([priced, unpriced])
        assert summary.unpriced_count == 1
        assert summary.priced_count == 1
        assert summary.cost_aud == priced.cost_aud
        assert summary.value_aud == priced.value_aud

    def test_zero_cost_has_no_percent(self):
        parcel = OpenParcel(
            buy_id=1,
            buy_date=date(2025, 1, 1),
            original_qty=Decimal(1),
            remaining_qty=Decimal(1),
            price=Decimal(0),
            cost_total_ccy=Decimal(0),
            cost_total_aud=Decimal(0),
            fx_aud_usd=Decimal("0.65"),
        )
        from app.services.shares import FifoResult

        valued = value_holding(
            FifoResult("ZZ", "USD", [parcel], []),
            Decimal(10),
            Decimal("0.65"),
        )
        assert valued.pct is None

    def test_aud_instrument_approx_usd(self):
        result = match_fifo(
            [
                _trade(
                    1,
                    "buy",
                    "2025-01-01",
                    "10",
                    "40",
                    fee="0",
                    fee_currency="AUD",
                    fx=None,
                    currency="AUD",
                    ticker="BHP.AX",
                )
            ]
        )
        valued = value_holding(result, Decimal(42), Decimal("0.65"))
        assert valued.approx_usd is True
        assert q2(valued.value_aud) == Decimal("420.00")
        assert q2(valued.value_usd) == Decimal("273.00")
        assert q2(valued.unrealised_aud) == Decimal("20.00")
        assert q2(valued.unrealised_usd) == Decimal("13.00")
        assert isinstance(valued, HoldingValuation)
