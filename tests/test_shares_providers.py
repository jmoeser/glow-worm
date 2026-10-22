"""Alpha Vantage, Frankfurter, and request-gate tests. No database."""

from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx

from app.services.market_data import (
    AV_URL,
    AlphaVantageFxProvider,
    AlphaVantagePriceProvider,
    BudgetExhaustedError,
    FrankfurterFxProvider,
    ProviderResponseError,
    RateLimitedError,
    RequestGate,
    SymbolNotFoundError,
)

API_KEY = "test-av-key"  # pragma: allowlist secret


def _client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=20.0)


def _daily_body(symbol: str = "IBM") -> str:
    return (
        '{"Meta Data": {"1. Information": "Daily Prices", "2. Symbol": "'
        + symbol
        + '", "3. Last Refreshed": "2026-10-16", "5. Time Zone": "US/Eastern"},'
        '"Time Series (Daily)": {'
        '"2026-10-16": {"1. open": "236.0500", "4. close": "232.1400", "5. volume": "1"},'
        '"2026-10-15": {"1. open": "230.0000", "4. close": "231.0000", "5. volume": "1"}'
        "}}"
    )


def _fx_body() -> str:
    return (
        '{"Meta Data": {"1. Information": "Forex Daily Prices", '
        '"2. From Symbol": "AUD", "3. To Symbol": "USD"},'
        '"Time Series FX (Daily)": {'
        '"2026-10-16": {"4. close": "0.69588"},'
        '"2026-10-15": {"4. close": "0.69000"}'
        "}}"
    )


class TestAlphaVantagePrices:
    def test_parses_daily_closes_newest_first(self):
        with respx.mock:
            route = respx.get(AV_URL).mock(
                return_value=httpx.Response(200, text=_daily_body())
            )
            provider = AlphaVantagePriceProvider(_client(), API_KEY)
            closes = provider.daily_closes("IBM")
            assert route.called
            params = respx.calls.last.request.url.params
            assert params["function"] == "TIME_SERIES_DAILY"
            assert params["outputsize"] == "compact"
            assert params["symbol"] == "IBM"
            assert closes[0].price_date == date(2026, 10, 16)
            assert closes[0].close == Decimal("232.1400")
            assert closes[1].close == Decimal("231.0000")
            assert closes[0].currency == "USD"
            assert isinstance(closes[0].close, Decimal)

    def test_ax_symbol_is_passed_through(self):
        with respx.mock:
            respx.get(AV_URL).mock(
                return_value=httpx.Response(200, text=_daily_body("BHP.AX"))
            )
            provider = AlphaVantagePriceProvider(_client(), API_KEY)
            closes = provider.daily_closes("bhp.ax")
            assert respx.calls.last.request.url.params["symbol"] == "BHP.AX"
            assert closes[0].ticker == "BHP.AX"
            assert closes[0].currency == "AUD"

    def test_aus_alias_maps_to_ax(self):
        assert AlphaVantagePriceProvider.to_provider_symbol("VAS.AUS") == "VAS.AX"


class TestAlphaVantageFx:
    def test_parses_aud_usd(self):
        with respx.mock:
            respx.get(AV_URL).mock(return_value=httpx.Response(200, text=_fx_body()))
            provider = AlphaVantageFxProvider(_client(), API_KEY)
            observations = provider.aud_usd()
            assert observations[0].rate == Decimal("0.69588")
            assert observations[0].source == "alphavantage"
            assert observations[0].rate_date == date(2026, 10, 16)
            params = respx.calls.last.request.url.params
            assert params["function"] == "FX_DAILY"
            assert params["from_symbol"] == "AUD"
            assert params["to_symbol"] == "USD"


class TestAlphaVantageErrors:
    def _provider(self) -> AlphaVantagePriceProvider:
        return AlphaVantagePriceProvider(_client(), API_KEY)

    def test_note_is_rate_limited(self):
        with respx.mock:
            respx.get(AV_URL).mock(
                return_value=httpx.Response(
                    200, json={"Note": "standard API rate limit is 25 requests per day"}
                )
            )
            with pytest.raises(RateLimitedError):
                self._provider().daily_closes("IBM")

    def test_information_is_rate_limited(self):
        with respx.mock:
            respx.get(AV_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={"Information": "The demo API key is for demo purposes only"},
                )
            )
            with pytest.raises(RateLimitedError):
                self._provider().daily_closes("IBM")

    def test_error_message_is_symbol_not_found(self):
        with respx.mock:
            respx.get(AV_URL).mock(
                return_value=httpx.Response(
                    200, json={"Error Message": "Invalid API call"}
                )
            )
            with pytest.raises(SymbolNotFoundError):
                self._provider().daily_closes("IBM")

    def test_empty_object(self):
        with respx.mock:
            respx.get(AV_URL).mock(return_value=httpx.Response(200, json={}))
            with pytest.raises(ProviderResponseError):
                self._provider().daily_closes("IBM")

    def test_http_500_redacts_key(self):
        with respx.mock:
            respx.get(AV_URL).mock(return_value=httpx.Response(500, text="nope"))
            with pytest.raises(ProviderResponseError) as caught:
                self._provider().daily_closes("IBM")
        assert API_KEY not in str(caught.value)
        assert caught.value.__cause__ is None
        assert "HTTP 500" in str(caught.value)

    def test_timeout_redacts_key(self):
        with respx.mock:
            respx.get(AV_URL).mock(side_effect=httpx.TimeoutException("timed out"))
            with pytest.raises(ProviderResponseError) as caught:
                self._provider().daily_closes("IBM")
        assert API_KEY not in str(caught.value)
        assert caught.value.__cause__ is None

    def test_invalid_close(self):
        body = '{"Time Series (Daily)": {"2026-10-16": {"4. close": "abc"}}}'
        with respx.mock:
            respx.get(AV_URL).mock(return_value=httpx.Response(200, text=body))
            with pytest.raises(ProviderResponseError):
                self._provider().daily_closes("IBM")


class TestFrankfurter:
    def test_v2_rate_is_decimal(self):
        payload = '{"date":"2026-10-16","base":"AUD","quote":"USD","rate":0.69588}'
        with respx.mock:
            respx.get("https://api.frankfurter.dev/v2/providers/ecb/rate/AUD/USD").mock(
                return_value=httpx.Response(200, text=payload)
            )
            provider = FrankfurterFxProvider(
                _client(), base_url="https://api.frankfurter.dev"
            )
            observations = provider.aud_usd()
        assert observations[0].rate == Decimal("0.69588")
        assert observations[0].source == "frankfurter"
        assert isinstance(observations[0].rate, Decimal)

    def test_404(self):
        with respx.mock:
            respx.get("https://api.frankfurter.dev/v2/providers/ecb/rate/AUD/USD").mock(
                return_value=httpx.Response(404, json={"message": "not found"})
            )
            provider = FrankfurterFxProvider(
                _client(), base_url="https://api.frankfurter.dev"
            )
            with pytest.raises(ProviderResponseError):
                provider.aud_usd()

    def test_follows_redirect_from_app_host(self):
        payload = '{"date":"2026-10-16","base":"AUD","quote":"USD","rate":0.69588}'
        with respx.mock:
            respx.get("https://api.frankfurter.app/v2/providers/ecb/rate/AUD/USD").mock(
                return_value=httpx.Response(
                    301,
                    headers={
                        "Location": "https://api.frankfurter.dev/v2/providers/ecb/rate/AUD/USD"
                    },
                )
            )
            respx.get("https://api.frankfurter.dev/v2/providers/ecb/rate/AUD/USD").mock(
                return_value=httpx.Response(200, text=payload)
            )
            provider = FrankfurterFxProvider(
                _client(), base_url="https://api.frankfurter.app"
            )
            observations = provider.aud_usd()
        assert observations[0].rate == Decimal("0.69588")


class TestRequestGate:
    def test_spacing_and_budget(self):
        clock = {"t": 0.0}
        sleeps: list[float] = []

        def now() -> float:
            return clock["t"]

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock["t"] += seconds

        gate = RequestGate(2, 13.0, sleep=sleep, clock=now)
        gate.acquire()
        assert sleeps == []
        clock["t"] = 5.0
        gate.acquire()
        assert sleeps == [8.0]
        with pytest.raises(BudgetExhaustedError):
            gate.acquire()
        assert gate.used == 2
