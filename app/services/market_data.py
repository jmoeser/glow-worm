"""Share-price and AUD/USD providers.

Frankfurter (ECB) is the primary daily AUD/USD source. Alpha Vantage
FX_DAILY is only used when Frankfurter fails. Share prices come from
Alpha Vantage TIME_SERIES_DAILY. The API key is never written into
exceptions, log lines, or stored run messages.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Protocol

import httpx

import app.config as app_config
from app.services.shares import instrument_currency, normalise_ticker

logger = logging.getLogger(__name__)

# httpx logs the full URL, including apikey, at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

AV_URL = "https://www.alphavantage.co/query"
_APIKEY_PARAM = re.compile(r"(?i)(apikey=)[^&\s\"']+")


def redact(text: str, key: str | None = None) -> str:
    """Remove API keys from a string before it is logged or stored."""
    if not text:
        return text
    configured = app_config.ALPHA_VANTAGE_API_KEY
    for secret in (key, configured):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return _APIKEY_PARAM.sub(r"\1[REDACTED]", text)


class MarketDataError(Exception):
    """Base error. The message must already be safe to log."""


class RateLimitedError(MarketDataError):
    """Alpha Vantage Note/Information. Stop calling Alpha Vantage this run."""


class SymbolNotFoundError(MarketDataError):
    """Alpha Vantage rejected the symbol."""


class ProviderResponseError(MarketDataError):
    """HTTP error, timeout, or a body that could not be parsed."""


class BudgetExhaustedError(MarketDataError):
    """The per-run Alpha Vantage request cap was reached."""


@dataclass(frozen=True)
class DailyClose:
    ticker: str
    price_date: date
    close: Decimal
    currency: str


@dataclass(frozen=True)
class FxObservation:
    base: str
    quote: str
    rate_date: date
    rate: Decimal
    source: str


class PriceProvider(Protocol):
    name: str

    def supports(self, ticker: str) -> bool: ...

    def daily_closes(self, ticker: str) -> list[DailyClose]: ...


class FxProvider(Protocol):
    name: str

    def aud_usd(self) -> list[FxObservation]: ...


class RequestGate:
    """Caps Alpha Vantage calls per run and spaces them out."""

    def __init__(
        self,
        max_requests: int,
        min_interval_s: float,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max_requests
        self._min_interval = min_interval_s
        self._sleep = sleep
        self._clock = clock
        self._used = 0
        self._last: float | None = None

    def acquire(self) -> None:
        if self._used >= self._max:
            raise BudgetExhaustedError(
                f"Alpha Vantage request budget of {self._max} exhausted"
            )
        now = self._clock()
        if self._last is not None:
            wait = self._min_interval - (now - self._last)
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._used += 1
        self._last = now

    @property
    def used(self) -> int:
        return self._used


def _user_agent() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        app_version = version("glow-worm")
    except PackageNotFoundError:
        app_version = "dev"
    return f"glow-worm/{app_version}"


def make_client() -> httpx.Client:
    return httpx.Client(
        follow_redirects=True,
        timeout=20.0,
        headers={"User-Agent": _user_agent()},
    )


class AlphaVantagePriceProvider:
    name = "alphavantage"

    def __init__(
        self,
        client: httpx.Client,
        api_key: str,
        gate: RequestGate | None = None,
    ) -> None:
        self.client = client
        self.api_key = api_key
        self.gate = gate

    def supports(self, ticker: str) -> bool:
        try:
            instrument_currency(ticker)
        except ValueError:
            return False
        return True

    @staticmethod
    def to_provider_symbol(ticker: str) -> str:
        symbol = normalise_ticker(ticker)
        if symbol.endswith(".AUS"):
            return symbol[: -len(".AUS")] + ".AX"
        return symbol

    def daily_closes(self, ticker: str) -> list[DailyClose]:
        symbol = self.to_provider_symbol(ticker)
        currency = instrument_currency(ticker)
        payload = self._get(
            {
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": "compact",
                "apikey": self.api_key,
            },
            symbol,
        )
        if "Error Message" in payload:
            raise SymbolNotFoundError(redact(str(payload["Error Message"]))[:200])
        series = payload.get("Time Series (Daily)")
        if not isinstance(series, dict) or not series:
            raise ProviderResponseError(f"No daily prices for {symbol}")
        closes: list[DailyClose] = []
        for day, bar in series.items():
            if not isinstance(bar, dict) or "4. close" not in bar:
                raise ProviderResponseError(f"Malformed price bar for {symbol}")
            try:
                close = Decimal(str(bar["4. close"]))
            except InvalidOperation:
                raise ProviderResponseError(
                    f"Invalid close for {symbol} on {day}"
                ) from None
            if not close.is_finite() or close <= 0:
                continue
            try:
                price_date = date.fromisoformat(str(day))
            except ValueError:
                raise ProviderResponseError(f"Invalid price date {day}") from None
            closes.append(
                DailyClose(
                    ticker=normalise_ticker(ticker),
                    price_date=price_date,
                    close=close,
                    currency=currency,
                )
            )
        if not closes:
            raise ProviderResponseError(f"No usable prices for {symbol}")
        closes.sort(key=lambda item: item.price_date, reverse=True)
        return closes

    def _get(self, params: dict[str, str], symbol: str) -> dict[str, object]:
        if self.gate is not None:
            self.gate.acquire()
        try:
            response = self.client.get(AV_URL, params=params)
        except httpx.TimeoutException:
            raise ProviderResponseError(
                f"Timeout from Alpha Vantage for {symbol}"
            ) from None
        except httpx.HTTPError:
            raise ProviderResponseError(
                f"Network error from Alpha Vantage for {symbol}"
            ) from None
        if response.status_code >= 400:
            raise ProviderResponseError(
                f"HTTP {response.status_code} from Alpha Vantage for {symbol}"
            )
        return _parse_av_body(response.text, symbol)


class AlphaVantageFxProvider:
    name = "alphavantage"

    def __init__(
        self,
        client: httpx.Client,
        api_key: str,
        gate: RequestGate | None = None,
    ) -> None:
        self._prices = AlphaVantagePriceProvider(client, api_key, gate)

    def aud_usd(self) -> list[FxObservation]:
        payload = self._prices._get(
            {
                "function": "FX_DAILY",
                "from_symbol": "AUD",
                "to_symbol": "USD",
                "outputsize": "compact",
                "apikey": self._prices.api_key,
            },
            "AUD/USD",
        )
        if "Error Message" in payload:
            raise ProviderResponseError(redact(str(payload["Error Message"]))[:200])
        series = payload.get("Time Series FX (Daily)")
        if not isinstance(series, dict) or not series:
            raise ProviderResponseError("No FX series from Alpha Vantage")
        observations: list[FxObservation] = []
        for day, bar in series.items():
            if not isinstance(bar, dict) or "4. close" not in bar:
                raise ProviderResponseError("Malformed FX bar from Alpha Vantage")
            try:
                rate = Decimal(str(bar["4. close"]))
                rate_date = date.fromisoformat(str(day))
            except InvalidOperation, ValueError:
                raise ProviderResponseError(
                    f"Invalid FX close from Alpha Vantage on {day}"
                ) from None
            if not rate.is_finite() or rate <= 0:
                continue
            observations.append(
                FxObservation(
                    base="AUD",
                    quote="USD",
                    rate_date=rate_date,
                    rate=rate,
                    source="alphavantage",
                )
            )
        if not observations:
            raise ProviderResponseError("No usable FX rates from Alpha Vantage")
        observations.sort(key=lambda item: item.rate_date, reverse=True)
        return observations


class FrankfurterFxProvider:
    name = "frankfurter"

    def __init__(
        self,
        client: httpx.Client,
        base_url: str | None = None,
    ) -> None:
        self.client = client
        self._base_url = base_url

    def _base(self) -> str:
        raw = (
            self._base_url
            if self._base_url is not None
            else app_config.SHARES_FRANKFURTER_BASE_URL
        )
        return raw.rstrip("/")

    def aud_usd(self) -> list[FxObservation]:
        url = f"{self._base()}/v2/providers/ecb/rate/AUD/USD"
        try:
            response = self.client.get(url)
        except httpx.TimeoutException:
            raise ProviderResponseError("Timeout from Frankfurter") from None
        except httpx.HTTPError:
            raise ProviderResponseError("Network error from Frankfurter") from None
        if response.status_code >= 400:
            raise ProviderResponseError(f"HTTP {response.status_code} from Frankfurter")
        try:
            payload = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError:
            raise ProviderResponseError("Malformed JSON from Frankfurter") from None
        if not isinstance(payload, dict):
            raise ProviderResponseError("Unexpected Frankfurter response")
        try:
            rate = Decimal(str(payload["rate"]))
            rate_date = date.fromisoformat(str(payload["date"]))
        except KeyError, InvalidOperation, ValueError:
            raise ProviderResponseError("Unexpected Frankfurter response") from None
        if not rate.is_finite() or rate <= 0:
            raise ProviderResponseError("Invalid Frankfurter rate")
        return [
            FxObservation(
                base="AUD",
                quote="USD",
                rate_date=rate_date,
                rate=rate,
                source="frankfurter",
            )
        ]


def _parse_av_body(body: str, symbol: str) -> dict[str, object]:
    try:
        payload = json.loads(body, parse_float=Decimal)
    except json.JSONDecodeError:
        raise ProviderResponseError(
            f"Malformed JSON from Alpha Vantage for {symbol}"
        ) from None
    if not isinstance(payload, dict):
        raise ProviderResponseError(
            f"Unexpected response from Alpha Vantage for {symbol}"
        )
    note = payload.get("Note")
    info = payload.get("Information")
    if note or info:
        raw = redact(str(note or info))[:200]
        logger.warning("Alpha Vantage stopped this run: %s", raw)
        raise RateLimitedError(raw)
    return payload


def build_price_provider(
    client: httpx.Client,
    gate: RequestGate | None = None,
) -> AlphaVantagePriceProvider | None:
    key = app_config.ALPHA_VANTAGE_API_KEY
    if not key:
        return None
    return AlphaVantagePriceProvider(client, key, gate)


def build_fx_providers(
    client: httpx.Client,
    gate: RequestGate | None = None,
) -> list[FxProvider]:
    """Frankfurter first. Alpha Vantage FX is the fallback when a key is set."""
    providers: list[FxProvider] = [FrankfurterFxProvider(client)]
    key = app_config.ALPHA_VANTAGE_API_KEY
    if key:
        providers.append(AlphaVantageFxProvider(client, key, gate))
    return providers
