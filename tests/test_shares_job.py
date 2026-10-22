"""Market-data job tests. Providers are fakes; the database is the test session."""

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import FxRate, MarketDataRun, SharePrice, ShareTrade
from app.services.market_data import (
    DailyClose,
    FxObservation,
    ProviderResponseError,
    RateLimitedError,
    RequestGate,
    SymbolNotFoundError,
)
from app.tasks import process_share_market_data, process_share_market_data_if_stale

API_KEY = "test-av-key"  # pragma: allowlist secret


def _trade(db, **overrides) -> ShareTrade:
    values = {
        "ticker": "AAPL",
        "trade_type": "buy",
        "trade_date": "2025-01-15",
        "quantity": Decimal(1),
        "price": Decimal(100),
        "currency": "USD",
        "fee": Decimal(0),
        "fee_currency": "USD",
        "fx_rate_aud_usd": Decimal("0.6500"),
        "fx_rate_input": Decimal("0.6500"),
        "fx_input_convention": "AUD/USD",
    }
    values.update(overrides)
    row = ShareTrade(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _close(ticker: str, day: str = "2026-10-16", price: str = "140") -> DailyClose:
    return DailyClose(
        ticker=ticker,
        price_date=date.fromisoformat(day),
        close=Decimal(price),
        currency="USD",
    )


class FakeFx:
    def __init__(self, name: str, observations=None, error=None):
        self.name = name
        self.observations = observations or []
        self.error = error
        self.calls = 0

    def aud_usd(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.observations


class FakePrices:
    def __init__(self, series, errors=None, gate=None):
        self.name = "alphavantage"
        self.series = series
        self.errors = errors or {}
        self.gate = gate
        self.calls: list[str] = []

    def supports(self, ticker: str) -> bool:
        return True

    def daily_closes(self, ticker: str):
        if self.gate is not None:
            self.gate.acquire()
        self.calls.append(ticker)
        if ticker in self.errors:
            raise self.errors[ticker]
        return self.series[ticker]


def _fx_obs(source: str = "frankfurter", rate: str = "0.69588") -> FxObservation:
    return FxObservation(
        base="AUD",
        quote="USD",
        rate_date=date(2026, 10, 16),
        rate=Decimal(rate),
        source=source,
    )


def _run(db, **kwargs):
    process_share_market_data(db, **kwargs)
    return db.query(MarketDataRun).order_by(MarketDataRun.id.desc()).first()


class TestShareMarketDataJob:
    def test_success_uses_frankfurter_and_prices(self, db_session):
        _trade(db_session, ticker="AAPL")
        _trade(db_session, ticker="MSFT")
        gate = RequestGate(10, 0, sleep=lambda _s: None)
        prices = FakePrices(
            {"AAPL": [_close("AAPL")], "MSFT": [_close("MSFT", price="400")]},
            gate=gate,
        )
        fx = FakeFx("frankfurter", [_fx_obs()])
        run = _run(
            db_session,
            price_provider=prices,
            fx_providers=[fx],
            gate=gate,
        )
        assert run.status == "success"
        assert run.fx_source == "frankfurter"
        assert run.av_requests == 2
        assert run.tickers_updated == 2
        assert db_session.query(FxRate).count() == 1
        assert db_session.query(SharePrice).count() == 2

    def test_fully_sold_ticker_is_not_fetched(self, db_session):
        _trade(db_session, ticker="SOLD", quantity=Decimal(2))
        _trade(
            db_session,
            ticker="SOLD",
            trade_type="sell",
            trade_date="2025-06-01",
            quantity=Decimal(2),
            price=Decimal(110),
        )
        _trade(db_session, ticker="HELD")
        gate = RequestGate(10, 0, sleep=lambda _s: None)
        prices = FakePrices({"HELD": [_close("HELD")]}, gate=gate)
        run = _run(
            db_session,
            price_provider=prices,
            fx_providers=[FakeFx("frankfurter", [_fx_obs()])],
            gate=gate,
        )
        assert prices.calls == ["HELD"]
        assert run.status == "success"

    def test_rerun_updates_changed_close_without_duplicates(self, db_session):
        _trade(db_session)
        gate = RequestGate(10, 0, sleep=lambda _s: None)
        prices = FakePrices({"AAPL": [_close("AAPL", price="140")]}, gate=gate)
        fx = [FakeFx("frankfurter", [_fx_obs()])]
        _run(db_session, price_provider=prices, fx_providers=fx, gate=gate)
        assert db_session.query(SharePrice).count() == 1
        prices.series["AAPL"] = [_close("AAPL", price="141")]
        _run(db_session, price_provider=prices, fx_providers=fx, gate=gate)
        rows = db_session.query(SharePrice).all()
        assert len(rows) == 1
        assert rows[0].close == Decimal(141)

    def test_price_rate_limit_keeps_frankfurter_and_stops(self, db_session):
        _trade(db_session, ticker="AAA")
        _trade(db_session, ticker="BBB")
        gate = RequestGate(10, 0, sleep=lambda _s: None)
        prices = FakePrices(
            {"AAA": [_close("AAA")], "BBB": [_close("BBB")]},
            errors={"BBB": RateLimitedError("slow down")},
            gate=gate,
        )
        run = _run(
            db_session,
            price_provider=prices,
            fx_providers=[FakeFx("frankfurter", [_fx_obs()])],
            gate=gate,
        )
        assert run.status == "partial"
        assert run.fx_source == "frankfurter"
        assert prices.calls == ["AAA", "BBB"]
        assert db_session.query(SharePrice).count() == 1

    def test_av_fx_fallback_when_frankfurter_fails(self, db_session):
        _trade(db_session)
        gate = RequestGate(10, 0, sleep=lambda _s: None)
        prices = FakePrices({"AAPL": [_close("AAPL")]}, gate=gate)
        av_fx = FakeFx("alphavantage", [_fx_obs("alphavantage", "0.66000")])
        run = _run(
            db_session,
            price_provider=prices,
            fx_providers=[
                FakeFx("frankfurter", error=ProviderResponseError("down")),
                av_fx,
            ],
            gate=gate,
        )
        assert run.fx_source == "alphavantage"
        assert run.status == "success"
        stored = db_session.query(FxRate).one()
        assert stored.source == "alphavantage"
        assert run.av_requests == 1

    def test_av_fx_rate_limit_skips_prices(self, db_session):
        _trade(db_session)
        prices = FakePrices({"AAPL": [_close("AAPL")]})
        run = _run(
            db_session,
            price_provider=prices,
            fx_providers=[
                FakeFx("frankfurter", error=ProviderResponseError("down")),
                FakeFx("alphavantage", error=RateLimitedError("quota")),
            ],
            gate=RequestGate(10, 0, sleep=lambda _s: None),
        )
        assert prices.calls == []
        assert run.fx_source is None
        assert run.status == "failed"

    def test_symbol_not_found_keeps_last_good_price(self, db_session):
        _trade(db_session, ticker="GOOD")
        _trade(db_session, ticker="BAD")
        db_session.add(
            SharePrice(
                ticker="BAD",
                price_date="2026-09-01",
                close=Decimal(10),
                currency="USD",
                source="alphavantage",
                fetched_at=datetime.now(UTC),
            )
        )
        db_session.commit()
        gate = RequestGate(10, 0, sleep=lambda _s: None)
        prices = FakePrices(
            {"GOOD": [_close("GOOD")], "BAD": []},
            errors={"BAD": SymbolNotFoundError("missing")},
            gate=gate,
        )
        run = _run(
            db_session,
            price_provider=prices,
            fx_providers=[FakeFx("frankfurter", [_fx_obs()])],
            gate=gate,
        )
        assert run.status == "partial"
        kept = db_session.query(SharePrice).filter(SharePrice.ticker == "BAD").one()
        assert kept.close == Decimal(10)
        assert kept.price_date == "2026-09-01"
        assert (
            db_session.query(SharePrice).filter(SharePrice.ticker == "GOOD").count()
            == 1
        )

    def test_budget_skips_the_freshest_tickers(self, db_session):
        for ticker, price_day in (
            ("NONE", None),
            ("OLD", "2020-01-02"),
            ("MID", "2024-06-01"),
            ("NEW1", "2026-10-01"),
            ("NEW2", "2026-10-15"),
        ):
            _trade(db_session, ticker=ticker)
            if price_day is not None:
                db_session.add(
                    SharePrice(
                        ticker=ticker,
                        price_date=price_day,
                        close=Decimal(1),
                        currency="USD",
                        source="alphavantage",
                        fetched_at=datetime.now(UTC),
                    )
                )
        db_session.commit()
        gate = RequestGate(3, 0, sleep=lambda _s: None)
        series = {
            ticker: [_close(ticker)]
            for ticker in ("NONE", "OLD", "MID", "NEW1", "NEW2")
        }
        prices = FakePrices(series, gate=gate)
        run = _run(
            db_session,
            price_provider=prices,
            fx_providers=[FakeFx("frankfurter", [_fx_obs()])],
            gate=gate,
        )
        assert prices.calls == ["NONE", "OLD", "MID"]
        assert "NEW1" in (run.message or "")
        assert "NEW2" in (run.message or "")
        assert run.status == "partial"

    def test_no_key_uses_frankfurter_and_is_partial(
        self, db_session, caplog, monkeypatch
    ):
        monkeypatch.setattr("app.config.ALPHA_VANTAGE_API_KEY", None)
        _trade(db_session)
        with caplog.at_level(logging.WARNING):
            run = _run(
                db_session,
                price_provider=None,
                fx_providers=[FakeFx("frankfurter", [_fx_obs()])],
                gate=RequestGate(5, 0, sleep=lambda _s: None),
            )
        assert run.status == "partial"
        assert run.fx_source == "frankfurter"
        assert "ALPHA_VANTAGE_API_KEY not set" in caplog.text
        assert db_session.query(SharePrice).count() == 0

    def test_no_trades_is_skipped(self, db_session):
        prices = FakePrices({})
        fx = FakeFx("frankfurter", [_fx_obs()])
        run = _run(db_session, price_provider=prices, fx_providers=[fx])
        assert run.status == "skipped"
        assert prices.calls == []
        assert fx.calls == 0

    def test_unexpected_error_fails_without_raising_and_redacts(
        self, db_session, caplog, monkeypatch
    ):
        monkeypatch.setattr("app.config.ALPHA_VANTAGE_API_KEY", API_KEY)
        _trade(db_session, ticker="AAA")
        _trade(db_session, ticker="ZZZ")
        gate = RequestGate(10, 0, sleep=lambda _s: None)

        class Exploding(FakePrices):
            def daily_closes(self, ticker: str):
                self.calls.append(ticker)
                if ticker == "ZZZ":
                    raise RuntimeError(f"boom {API_KEY}")
                return self.series[ticker]

        prices = Exploding({"AAA": [_close("AAA")], "ZZZ": []}, gate=None)
        with caplog.at_level(logging.ERROR):
            process_share_market_data(
                db_session,
                price_provider=prices,
                fx_providers=[FakeFx("frankfurter", [_fx_obs()])],
                gate=gate,
            )
        run = db_session.query(MarketDataRun).order_by(MarketDataRun.id.desc()).one()
        assert run.status == "failed"
        assert API_KEY not in caplog.text
        assert API_KEY not in (run.message or "")
        kept = db_session.query(SharePrice).filter(SharePrice.ticker == "AAA").one()
        assert kept.close == Decimal(140)

    def test_catchup_skips_a_recent_run(self, db_session):
        _trade(db_session)
        db_session.add(
            MarketDataRun(
                status="success",
                started_at=datetime.now(UTC) - timedelta(hours=1),
                finished_at=datetime.now(UTC),
                av_requests=0,
                tickers_updated=0,
            )
        )
        db_session.commit()
        prices = FakePrices({"AAPL": [_close("AAPL")]})
        process_share_market_data_if_stale(
            db_session,
            price_provider=prices,
            fx_providers=[FakeFx("frankfurter", [_fx_obs()])],
        )
        assert prices.calls == []

    def test_catchup_runs_when_last_attempt_is_old(self, db_session):
        _trade(db_session)
        db_session.add(
            MarketDataRun(
                status="success",
                started_at=datetime.now(UTC) - timedelta(hours=21),
                finished_at=datetime.now(UTC) - timedelta(hours=21),
                av_requests=0,
                tickers_updated=0,
            )
        )
        db_session.commit()
        gate = RequestGate(5, 0, sleep=lambda _s: None)
        prices = FakePrices({"AAPL": [_close("AAPL")]}, gate=gate)
        process_share_market_data_if_stale(
            db_session,
            price_provider=prices,
            fx_providers=[FakeFx("frankfurter", [_fx_obs()])],
            gate=gate,
        )
        assert prices.calls == ["AAPL"]

    def test_scheduler_registers_the_share_job(self):
        from app.scheduler import register_jobs

        calls: list[dict] = []

        class _Recorder:
            def add_job(self, *args, **kwargs):
                calls.append(kwargs)

        register_jobs(_Recorder())
        ids = [call.get("id") for call in calls]
        assert "share_market_data" in ids
        assert "share_market_data_catchup" in ids
        share = next(call for call in calls if call.get("id") == "share_market_data")
        assert share["max_instances"] == 1
        assert share["misfire_grace_time"] == 3600


class TestConfigFallbacks:
    def test_bad_time_falls_back(self, caplog):
        from app.config import _parse_hhmm

        with caplog.at_level(logging.WARNING):
            assert _parse_hhmm("nope") == (18, 0)
        assert "SHARES_UPDATE_TIME" in caplog.text

    def test_out_of_range_int_falls_back(self, caplog):
        from app.config import _int_env

        with caplog.at_level(logging.WARNING), pytest.MonkeyPatch.context() as patch:
            patch.setenv("SHARES_AV_MAX_REQUESTS_PER_RUN", "99")
            assert _int_env("SHARES_AV_MAX_REQUESTS_PER_RUN", 20, lo=1, hi=25) == 20
        assert "SHARES_AV_MAX_REQUESTS_PER_RUN" in caplog.text
