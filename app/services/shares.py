"""FIFO share accounting and portfolio valuation.

Pure calculations live at the top of this module. Database reads are
grouped at the bottom and are not required by the unit tests.

Same-day trades are processed by trade date, then by entry order (id).
A sell recorded before a same-day buy is an oversell.
"""

from __future__ import annotations

import calendar
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.orm import Session

AUD_USD_MIN = Decimal("0.40")
AUD_USD_MAX = Decimal("1.25")
USD_AUD_MIN = Decimal("0.80")
USD_AUD_MAX = Decimal("2.50")
FX_QUANT = Decimal("0.0000000001")
CENT = Decimal("0.01")
QTY_QUANT = Decimal("0.000001")
TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,19}$")

_SOURCE_LABELS = {
    "frankfurter": "Frankfurter",
    "alphavantage": "Alpha Vantage",
}


class FxRateError(ValueError):
    """Raised when an FX rate is missing, non-finite, or implausible."""


class OversellError(ValueError):
    """A sell needs more shares than FIFO still has open."""

    def __init__(
        self,
        ticker: str,
        available: Decimal,
        *,
        trade_id: int | None = None,
        trade_date: date | None = None,
    ) -> None:
        self.ticker = ticker
        self.available = available
        self.trade_id = trade_id
        self.trade_date = trade_date
        when = trade_date.isoformat() if trade_date is not None else "that date"
        super().__init__(
            f"Sell of {ticker} on {when} exceeds holdings "
            f"({format_qty(available)} available)."
        )


def q2(value: Decimal) -> Decimal:
    """Quantize to cents, half up. Display and JSON only."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def q_fx(value: Decimal) -> Decimal:
    return value.quantize(FX_QUANT, rounding=ROUND_HALF_UP)


def format_qty(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_fx(value: Decimal) -> str:
    """At least 4 decimal places, up to the stored 10."""
    text = f"{q_fx(value):.10f}".rstrip("0")
    whole, _, frac = text.partition(".")
    if len(frac) < 4:
        frac = frac.ljust(4, "0")
    return f"{whole}.{frac}"


def normalise_ticker(ticker: str) -> str:
    return ticker.strip().upper()


def instrument_currency(ticker: str) -> str:
    """USD for a bare ticker, AUD for .AX / .AUS. Any other suffix is rejected."""
    symbol = normalise_ticker(ticker)
    if symbol.endswith((".AX", ".AUS")):
        return "AUD"
    if "." in symbol:
        raise ValueError("Only US tickers and ASX (.AX) tickers are supported.")
    return "USD"


def normalise_fx(value: Decimal, convention: str) -> Decimal:
    """Return AUD per the stored convention (USD for 1 AUD)."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if not value.is_finite() or value <= 0:
        raise FxRateError("FX rate must be a positive number.")
    kind = convention.strip().upper()
    if kind == "AUD/USD":
        if value < AUD_USD_MIN or value > AUD_USD_MAX:
            raise FxRateError(_fx_hint(value, "AUD/USD"))
        return q_fx(value)
    if kind == "USD/AUD":
        if value < USD_AUD_MIN or value > USD_AUD_MAX:
            raise FxRateError(_fx_hint(value, "USD/AUD"))
        return q_fx(Decimal(1) / value)
    raise FxRateError("FX convention must be AUD/USD or USD/AUD.")


def _fx_hint(value: Decimal, entered_as: str) -> str:
    shown = format(value, "f")
    if entered_as == "AUD/USD" and USD_AUD_MIN <= value <= USD_AUD_MAX:
        reciprocal = q_fx(Decimal(1) / value)
        return (
            f"{shown} looks like a USD/AUD rate. "
            f"Select USD/AUD or enter {format_fx(reciprocal)}."
        )
    if entered_as == "USD/AUD" and AUD_USD_MIN <= value <= AUD_USD_MAX:
        return (
            f"{shown} looks like an AUD/USD rate. "
            "Select AUD/USD or enter the USD per AUD rate."
        )
    if entered_as == "AUD/USD":
        return (
            f"AUD/USD rate {shown} is outside the expected range "
            f"({AUD_USD_MIN}–{AUD_USD_MAX}). "
            "If this is a USD/AUD rate, select USD/AUD."
        )
    return (
        f"USD/AUD rate {shown} is outside the expected range "
        f"({USD_AUD_MIN}–{USD_AUD_MAX}). "
        "If this is an AUD/USD rate, select AUD/USD."
    )


def fee_in_trade_ccy(
    fee: Decimal,
    fee_currency: str,
    trade_currency: str,
    fx_aud_usd: Decimal | None,
) -> Decimal:
    """Express a brokerage fee in the trade's currency.

    An AUD fee on a USD trade converts at the trade's AUD/USD rate
    (USD = AUD × rate). A USD fee on an AUD trade divides by that rate.
    """
    fee_ccy = fee_currency.strip().upper()
    trade_ccy = trade_currency.strip().upper()
    if fee_ccy == trade_ccy:
        return fee
    if fx_aud_usd is None or fx_aud_usd <= 0:
        raise FxRateError("An AUD/USD rate is required to convert the brokerage fee.")
    if fee_ccy == "AUD" and trade_ccy == "USD":
        return fee * fx_aud_usd
    if fee_ccy == "USD" and trade_ccy == "AUD":
        return fee / fx_aud_usd
    raise FxRateError(f"Unsupported fee currency {fee_currency}.")


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def held_over_12_months(buy: date, sell: date) -> bool:
    """True when the disposal date is strictly after the 12-month anniversary.

    The acquisition day is excluded, matching the ATO 'more than 12 months'
    test. 29 February rolls back to 28 February in a non-leap anniversary.
    """
    return _add_months(buy, 12) < sell


def australian_financial_year(day: date) -> str:
    """Australian financial year label, e.g. FY2025-26 for 1 Jul 2025–30 Jun 2026."""
    start = day.year if day.month >= 7 else day.year - 1
    end_yy = (start + 1) % 100
    return f"FY{start}-{end_yy:02d}"


@dataclass(frozen=True)
class TradeIn:
    id: int
    ticker: str
    trade_type: str
    trade_date: date
    quantity: Decimal
    price: Decimal
    currency: str
    fee: Decimal
    fee_currency: str
    fx_aud_usd: Decimal | None


@dataclass
class OpenParcel:
    buy_id: int
    buy_date: date
    original_qty: Decimal
    remaining_qty: Decimal
    price: Decimal
    cost_total_ccy: Decimal
    cost_total_aud: Decimal
    fx_aud_usd: Decimal | None = None

    @property
    def remaining_cost_aud(self) -> Decimal:
        if self.original_qty == 0:
            return Decimal(0)
        return self.cost_total_aud * self.remaining_qty / self.original_qty

    @property
    def remaining_cost_ccy(self) -> Decimal:
        if self.original_qty == 0:
            return Decimal(0)
        return self.cost_total_ccy * self.remaining_qty / self.original_qty


@dataclass(frozen=True)
class RealisedMatch:
    sell_id: int
    sell_date: date
    buy_id: int
    buy_date: date
    quantity: Decimal
    proceeds_ccy: Decimal
    cost_ccy: Decimal
    proceeds_aud: Decimal
    cost_aud: Decimal
    gain_aud: Decimal
    gain_ccy: Decimal
    held_over_12_months: bool
    financial_year: str


@dataclass
class FifoResult:
    ticker: str
    currency: str
    open_parcels: list[OpenParcel]
    realised: list[RealisedMatch]


def match_fifo(trades: list[TradeIn]) -> FifoResult:
    """Match sells to buys first-in-first-out for a single ticker.

    Sort key is (trade_date, id): entry order, not buy-before-sell.
    """
    if not trades:
        return FifoResult(ticker="", currency="", open_parcels=[], realised=[])

    ordered = sorted(trades, key=lambda trade: (trade.trade_date, trade.id))
    ticker = ordered[0].ticker
    currency = ordered[0].currency
    queue: deque[OpenParcel] = deque()
    realised: list[RealisedMatch] = []

    for trade in ordered:
        if trade.ticker != ticker:
            raise ValueError("match_fifo expects trades for a single ticker.")
        if trade.trade_type == "buy":
            queue.append(_parcel_from_buy(trade))
            continue
        if trade.trade_type != "sell":
            raise ValueError(f"Unknown trade type {trade.trade_type}.")
        available = sum((parcel.remaining_qty for parcel in queue), Decimal(0))
        if trade.quantity > available:
            raise OversellError(
                trade.ticker,
                available,
                trade_id=trade.id,
                trade_date=trade.trade_date,
            )
        fee_ccy = fee_in_trade_ccy(
            trade.fee, trade.fee_currency, trade.currency, trade.fx_aud_usd
        )
        to_match = trade.quantity
        while to_match > 0:
            parcel = queue[0]
            matched = min(parcel.remaining_qty, to_match)
            if matched <= 0:
                raise OversellError(
                    trade.ticker,
                    Decimal(0),
                    trade_id=trade.id,
                    trade_date=trade.trade_date,
                )
            cost_ccy = parcel.cost_total_ccy * matched / parcel.original_qty
            cost_aud = parcel.cost_total_aud * matched / parcel.original_qty
            fee_part = fee_ccy * matched / trade.quantity
            proceeds_ccy = matched * trade.price - fee_part
            proceeds_aud = _to_aud(proceeds_ccy, trade.currency, trade.fx_aud_usd)
            realised.append(
                RealisedMatch(
                    sell_id=trade.id,
                    sell_date=trade.trade_date,
                    buy_id=parcel.buy_id,
                    buy_date=parcel.buy_date,
                    quantity=matched,
                    proceeds_ccy=proceeds_ccy,
                    cost_ccy=cost_ccy,
                    proceeds_aud=proceeds_aud,
                    cost_aud=cost_aud,
                    gain_aud=proceeds_aud - cost_aud,
                    gain_ccy=proceeds_ccy - cost_ccy,
                    held_over_12_months=held_over_12_months(
                        parcel.buy_date, trade.trade_date
                    ),
                    financial_year=australian_financial_year(trade.trade_date),
                )
            )
            parcel.remaining_qty -= matched
            to_match -= matched
            if parcel.remaining_qty <= 0:
                queue.popleft()

    return FifoResult(
        ticker=ticker,
        currency=currency,
        open_parcels=list(queue),
        realised=realised,
    )


def _parcel_from_buy(trade: TradeIn) -> OpenParcel:
    fee_ccy = fee_in_trade_ccy(
        trade.fee, trade.fee_currency, trade.currency, trade.fx_aud_usd
    )
    cost_ccy = trade.quantity * trade.price + fee_ccy
    cost_aud = _to_aud(cost_ccy, trade.currency, trade.fx_aud_usd)
    return OpenParcel(
        buy_id=trade.id,
        buy_date=trade.trade_date,
        original_qty=trade.quantity,
        remaining_qty=trade.quantity,
        price=trade.price,
        cost_total_ccy=cost_ccy,
        cost_total_aud=cost_aud,
        fx_aud_usd=trade.fx_aud_usd,
    )


def _to_aud(amount: Decimal, currency: str, fx_aud_usd: Decimal | None) -> Decimal:
    if currency == "AUD":
        return amount
    if fx_aud_usd is None or fx_aud_usd <= 0:
        raise FxRateError("An AUD/USD rate is required for USD trades.")
    return amount / fx_aud_usd


@dataclass
class HoldingValuation:
    ticker: str
    currency: str
    quantity: Decimal
    cost_aud: Decimal
    cost_ccy: Decimal
    cost_usd: Decimal | None
    value_ccy: Decimal | None
    value_aud: Decimal | None
    value_usd: Decimal | None
    unrealised_aud: Decimal | None
    unrealised_usd: Decimal | None
    pct: Decimal | None
    has_price: bool
    approx_usd: bool
    open_parcels: list[OpenParcel]
    realised: list[RealisedMatch]
    price: Decimal | None = None
    price_date: str | None = None


def value_holding(
    result: FifoResult,
    close: Decimal | None,
    fx_aud_usd_now: Decimal | None,
    *,
    price_date: str | None = None,
) -> HoldingValuation:
    """Value open parcels. AUD instruments translate USD at today's rate (≈ USD)."""
    quantity = sum((parcel.remaining_qty for parcel in result.open_parcels), Decimal(0))
    cost_aud = sum(
        (parcel.remaining_cost_aud for parcel in result.open_parcels), Decimal(0)
    )
    cost_ccy = sum(
        (parcel.remaining_cost_ccy for parcel in result.open_parcels), Decimal(0)
    )
    has_price = close is not None
    value_ccy = quantity * close if close is not None else None
    approx_usd = result.currency == "AUD"
    value_aud: Decimal | None = None
    value_usd: Decimal | None = None
    cost_usd: Decimal | None = None
    unrealised_aud: Decimal | None = None
    unrealised_usd: Decimal | None = None

    fx_ok = fx_aud_usd_now is not None and fx_aud_usd_now > 0
    if result.currency == "USD":
        cost_usd = cost_ccy
        if value_ccy is not None:
            value_usd = value_ccy
            if fx_ok:
                assert fx_aud_usd_now is not None
                value_aud = value_ccy / fx_aud_usd_now
                unrealised_aud = value_aud - cost_aud
                unrealised_usd = value_usd - cost_usd
    else:
        if fx_ok:
            assert fx_aud_usd_now is not None
            cost_usd = cost_aud * fx_aud_usd_now
        if value_ccy is not None:
            value_aud = value_ccy
            unrealised_aud = value_aud - cost_aud
            if fx_ok:
                assert fx_aud_usd_now is not None
                value_usd = value_ccy * fx_aud_usd_now
                assert cost_usd is not None
                unrealised_usd = value_usd - cost_usd

    pct: Decimal | None = None
    if unrealised_aud is not None and cost_aud != 0:
        pct = unrealised_aud / cost_aud * Decimal(100)

    return HoldingValuation(
        ticker=result.ticker,
        currency=result.currency,
        quantity=quantity,
        cost_aud=cost_aud,
        cost_ccy=cost_ccy,
        cost_usd=cost_usd,
        value_ccy=value_ccy,
        value_aud=value_aud,
        value_usd=value_usd,
        unrealised_aud=unrealised_aud,
        unrealised_usd=unrealised_usd,
        pct=pct,
        has_price=has_price,
        approx_usd=approx_usd,
        open_parcels=list(result.open_parcels),
        realised=list(result.realised),
        price=close,
        price_date=price_date,
    )


@dataclass
class PortfolioSummary:
    cost_aud: Decimal
    value_aud: Decimal
    value_usd: Decimal
    unrealised_aud: Decimal
    unrealised_usd: Decimal
    realised_aud: Decimal
    unpriced_count: int
    priced_count: int


def summarise_portfolio(
    valuations: list[HoldingValuation],
    *,
    financial_year: str | None = None,
) -> PortfolioSummary:
    """Sum holdings that have a price and an AUD value.

    Holdings with no price are counted and left out of the totals.
    Realised gains are summed from every valuation (a missing price does
    not change a gain that was already locked in).
    """
    cost_aud = Decimal(0)
    value_aud = Decimal(0)
    value_usd = Decimal(0)
    unrealised_aud = Decimal(0)
    unrealised_usd = Decimal(0)
    realised_aud = Decimal(0)
    unpriced = 0
    priced = 0
    for holding in valuations:
        for match in holding.realised:
            if financial_year is None or match.financial_year == financial_year:
                realised_aud += match.gain_aud
        if not holding.has_price:
            unpriced += 1
            continue
        if holding.value_aud is None or holding.value_usd is None:
            continue
        if holding.unrealised_aud is None or holding.unrealised_usd is None:
            continue
        priced += 1
        cost_aud += holding.cost_aud
        value_aud += holding.value_aud
        value_usd += holding.value_usd
        unrealised_aud += holding.unrealised_aud
        unrealised_usd += holding.unrealised_usd
    return PortfolioSummary(
        cost_aud=cost_aud,
        value_aud=value_aud,
        value_usd=value_usd,
        unrealised_aud=unrealised_aud,
        unrealised_usd=unrealised_usd,
        realised_aud=realised_aud,
        unpriced_count=unpriced,
        priced_count=priced,
    )


def parse_decimal(raw: object, field_name: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        raise ValueError(f"Invalid {field_name}.") from None
    if not value.is_finite():
        raise ValueError(f"Invalid {field_name}.")
    return value


def preview_message(
    *,
    ticker: str,
    trade_type: str,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    fee_currency: str,
    fx_rate: Decimal | None,
    fx_convention: str | None,
) -> str:
    """One-line preview for the trade form. Never calls a market-data API."""
    currency = instrument_currency(ticker)
    kind = trade_type if trade_type in {"buy", "sell"} else "buy"
    if currency == "AUD":
        amount = quantity * price + (fee if kind == "buy" else -fee)
        label = "Cost base" if kind == "buy" else "Proceeds"
        return f"{label} A${q2(amount):,.2f} (AUD instrument, no FX)."
    if fx_rate is None or fx_convention is None:
        return "Enter the broker FX rate to preview the AUD cost base."
    stored = normalise_fx(fx_rate, fx_convention)
    fee_ccy = fee_in_trade_ccy(fee, fee_currency, currency, stored)
    amount_ccy = quantity * price + (fee_ccy if kind == "buy" else -fee_ccy)
    amount_aud = amount_ccy / stored
    label = "Cost base" if kind == "buy" else "Proceeds"
    return f"Stored as AUD/USD {format_fx(stored)} · {label} ≈ A${q2(amount_aud):,.2f}"


# ---------------------------------------------------------------------------
# Persistence adapters and the page view model
# ---------------------------------------------------------------------------


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return _as_decimal(value)


def trade_to_in(row: object) -> TradeIn:
    from app.models import ShareTrade

    if not isinstance(row, ShareTrade):
        raise TypeError("Expected a ShareTrade row.")
    return TradeIn(
        id=row.id,
        ticker=row.ticker,
        trade_type=row.trade_type,
        trade_date=date.fromisoformat(row.trade_date),
        quantity=_as_decimal(row.quantity),
        price=_as_decimal(row.price),
        currency=row.currency,
        fee=_as_decimal(row.fee),
        fee_currency=row.fee_currency,
        fx_aud_usd=_as_optional_decimal(row.fx_rate_aud_usd),
    )


def load_trades(db: Session, ticker: str | None = None) -> dict[str, list[TradeIn]]:
    from app.models import ShareTrade

    query = db.query(ShareTrade)
    if ticker is not None:
        query = query.filter(ShareTrade.ticker == ticker)
    rows = query.order_by(ShareTrade.trade_date, ShareTrade.id).all()
    grouped: dict[str, list[TradeIn]] = {}
    for row in rows:
        grouped.setdefault(row.ticker, []).append(trade_to_in(row))
    return grouped


@dataclass(frozen=True)
class StoredPrice:
    close: Decimal
    price_date: str
    fetched_at: datetime
    currency: str


@dataclass(frozen=True)
class StoredFx:
    rate: Decimal
    rate_date: str
    fetched_at: datetime
    source: str


def latest_prices(db: Session) -> dict[str, StoredPrice]:
    from app.models import SharePrice

    rows = (
        db.query(SharePrice)
        .order_by(SharePrice.ticker, SharePrice.price_date.desc())
        .all()
    )
    found: dict[str, StoredPrice] = {}
    for row in rows:
        if row.ticker in found:
            continue
        found[row.ticker] = StoredPrice(
            close=_as_decimal(row.close),
            price_date=row.price_date,
            fetched_at=row.fetched_at,
            currency=row.currency,
        )
    return found


def latest_price(db: Session, ticker: str) -> tuple[Decimal, str, datetime] | None:
    quote = latest_prices(db).get(ticker)
    if quote is None:
        return None
    return quote.close, quote.price_date, quote.fetched_at


def latest_fx(db: Session) -> tuple[Decimal, str, datetime, str] | None:
    quote = latest_fx_quote(db)
    if quote is None:
        return None
    return quote.rate, quote.rate_date, quote.fetched_at, quote.source


def latest_fx_quote(db: Session) -> StoredFx | None:
    from app.models import FxRate

    row = (
        db.query(FxRate)
        .filter(FxRate.base == "AUD", FxRate.quote == "USD")
        .order_by(FxRate.rate_date.desc(), FxRate.id.desc())
        .first()
    )
    if row is None:
        return None
    return StoredFx(
        rate=_as_decimal(row.rate),
        rate_date=row.rate_date,
        fetched_at=row.fetched_at,
        source=row.source,
    )


def held_tickers(db: Session) -> list[str]:
    """Tickers that still have remaining quantity, in ticker order."""
    held: list[str] = []
    for ticker, trades in sorted(load_trades(db).items()):
        result = match_fifo(trades)
        if any(parcel.remaining_qty > 0 for parcel in result.open_parcels):
            held.append(ticker)
    return held


@dataclass
class ParcelView:
    buy_date: str
    remaining_qty: Decimal
    price: Decimal
    fx: Decimal | None
    cost_aud: Decimal
    cost_ccy: Decimal


@dataclass
class HoldingView:
    ticker: str
    currency: str
    quantity: Decimal
    cost_aud: Decimal
    cost_ccy: Decimal
    price: Decimal | None
    price_date: str | None
    price_label: str
    value_usd: Decimal | None
    value_aud: Decimal | None
    unrealised_aud: Decimal | None
    unrealised_usd: Decimal | None
    pct: Decimal | None
    approx_usd: bool
    parcels: list[ParcelView]


@dataclass
class RealisedRowView:
    sell_date: str
    ticker: str
    quantity: Decimal
    buy_date: str
    proceeds_aud: Decimal
    cost_aud: Decimal
    gain_aud: Decimal
    gain_ccy: Decimal
    currency: str
    held_over_12_months: bool


@dataclass
class RealisedGroupView:
    financial_year: str
    total_aud: Decimal
    total_ccy: Decimal
    rows: list[RealisedRowView]


@dataclass
class SummaryView:
    cost_aud: Decimal
    value_aud: Decimal
    value_usd: Decimal
    unrealised_aud: Decimal
    unrealised_usd: Decimal
    unrealised_pct: Decimal | None
    realised_fy_aud: Decimal
    financial_year: str
    unpriced_count: int


@dataclass
class FxView:
    rate: Decimal
    rate_display: str
    rate_date: str
    source: str
    source_label: str
    fetched_local: str


@dataclass
class RunView:
    status: str
    when_local: str
    message: str


@dataclass
class SharesPage:
    holdings: list[HoldingView]
    realised_groups: list[RealisedGroupView]
    summary: SummaryView
    trades: list[object] = field(default_factory=list)
    fx: FxView | None = None
    price_as_of: str | None = None
    price_fetched_local: str | None = None
    banners: list[str] = field(default_factory=list)
    api_key_configured: bool = False
    today: str = ""
    has_usd_holding: bool = False


def _as_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def format_local(moment: datetime) -> str:
    from app.config import TIMEZONE

    local = _as_utc(moment).astimezone(TIMEZONE)
    return local.strftime("%Y-%m-%d %H:%M %Z")


def _price_label(currency: str) -> str:
    if currency == "AUD":
        return "ASX close"
    return "US close"


def build_shares_page(db: Session, *, today: date | None = None) -> SharesPage:
    from app.config import ALPHA_VANTAGE_API_KEY, TIMEZONE
    from app.models import MarketDataRun, ShareTrade

    if today is None:
        today = datetime.now(TIMEZONE).date()
    fy = australian_financial_year(today)
    grouped = load_trades(db)
    prices = latest_prices(db)
    fx_quote = latest_fx_quote(db)
    fx_now = fx_quote.rate if fx_quote is not None else None

    valuations: list[HoldingValuation] = []
    realised_rows: list[tuple[RealisedMatch, str, str]] = []
    for ticker, trades in sorted(grouped.items()):
        result = match_fifo(trades)
        for match in result.realised:
            realised_rows.append((match, result.currency, result.ticker))
        if not any(parcel.remaining_qty > 0 for parcel in result.open_parcels):
            continue
        quote = prices.get(ticker)
        valuations.append(
            value_holding(
                result,
                quote.close if quote is not None else None,
                fx_now,
                price_date=quote.price_date if quote is not None else None,
            )
        )

    summary = summarise_portfolio(valuations, financial_year=fy)
    holdings = [_present_holding(item) for item in valuations]
    groups = _present_realised(realised_rows)
    fy_total = next(
        (group.total_aud for group in groups if group.financial_year == fy),
        Decimal(0),
    )
    pct: Decimal | None = None
    if summary.priced_count and summary.cost_aud != 0:
        pct = q2(summary.unrealised_aud / summary.cost_aud * Decimal(100))

    price_dates = [
        holding.price_date for holding in holdings if holding.price_date is not None
    ]
    price_as_of: str | None = None
    if price_dates:
        earliest, latest = min(price_dates), max(price_dates)
        price_as_of = earliest if earliest == latest else f"{earliest} – {latest}"

    fetched_times = [quote.fetched_at for quote in prices.values()]
    if fx_quote is not None:
        fetched_times.append(fx_quote.fetched_at)
    price_fetched = format_local(max(fetched_times)) if fetched_times else None

    fx_view = None
    if fx_quote is not None:
        fx_view = FxView(
            rate=fx_quote.rate,
            rate_display=format_fx(fx_quote.rate),
            rate_date=fx_quote.rate_date,
            source=fx_quote.source,
            source_label=_SOURCE_LABELS.get(fx_quote.source, fx_quote.source),
            fetched_local=format_local(fx_quote.fetched_at),
        )

    banners = _banners(
        today=today,
        holdings=holdings,
        fx_quote=fx_quote,
        api_key=ALPHA_VANTAGE_API_KEY,
        unpriced=summary.unpriced_count,
    )
    run = db.query(MarketDataRun).order_by(MarketDataRun.id.desc()).first()
    if run is not None and run.status in {"failed", "partial"}:
        when = format_local(run.finished_at or run.started_at)
        detail = (run.message or "").strip()
        text = f"Last update attempt {when}: {run.status}."
        if detail:
            text = f"{text} {detail}"
        banners.insert(0, text)

    trades = (
        db.query(ShareTrade)
        .order_by(ShareTrade.trade_date.desc(), ShareTrade.id.desc())
        .all()
    )
    return SharesPage(
        holdings=holdings,
        realised_groups=groups,
        summary=SummaryView(
            cost_aud=q2(summary.cost_aud),
            value_aud=q2(summary.value_aud),
            value_usd=q2(summary.value_usd),
            unrealised_aud=q2(summary.unrealised_aud),
            unrealised_usd=q2(summary.unrealised_usd),
            unrealised_pct=pct,
            realised_fy_aud=fy_total,
            financial_year=fy,
            unpriced_count=summary.unpriced_count,
        ),
        trades=list(trades),
        fx=fx_view,
        price_as_of=price_as_of,
        price_fetched_local=price_fetched,
        banners=banners,
        api_key_configured=bool(ALPHA_VANTAGE_API_KEY),
        today=today.isoformat(),
        has_usd_holding=any(holding.currency == "USD" for holding in holdings),
    )


def _present_holding(holding: HoldingValuation) -> HoldingView:
    parcels = [
        ParcelView(
            buy_date=parcel.buy_date.isoformat(),
            remaining_qty=parcel.remaining_qty,
            price=parcel.price,
            fx=parcel.fx_aud_usd,
            cost_aud=q2(parcel.remaining_cost_aud),
            cost_ccy=q2(parcel.remaining_cost_ccy),
        )
        for parcel in holding.open_parcels
    ]
    return HoldingView(
        ticker=holding.ticker,
        currency=holding.currency,
        quantity=holding.quantity,
        cost_aud=q2(holding.cost_aud),
        cost_ccy=q2(holding.cost_ccy),
        price=holding.price,
        price_date=holding.price_date,
        price_label=_price_label(holding.currency),
        value_usd=q2(holding.value_usd) if holding.value_usd is not None else None,
        value_aud=q2(holding.value_aud) if holding.value_aud is not None else None,
        unrealised_aud=(
            q2(holding.unrealised_aud) if holding.unrealised_aud is not None else None
        ),
        unrealised_usd=(
            q2(holding.unrealised_usd) if holding.unrealised_usd is not None else None
        ),
        pct=q2(holding.pct) if holding.pct is not None else None,
        approx_usd=holding.approx_usd,
        parcels=parcels,
    )


def _present_realised(
    rows: list[tuple[RealisedMatch, str, str]],
) -> list[RealisedGroupView]:
    grouped: dict[str, list[tuple[RealisedMatch, str, str]]] = defaultdict(list)
    for item in rows:
        grouped[item[0].financial_year].append(item)
    groups: list[RealisedGroupView] = []
    for fy in sorted(grouped, reverse=True):
        items = sorted(
            grouped[fy],
            key=lambda item: (item[0].sell_date, item[0].sell_id),
            reverse=True,
        )
        view_rows = [
            RealisedRowView(
                sell_date=match.sell_date.isoformat(),
                ticker=ticker,
                quantity=match.quantity,
                buy_date=match.buy_date.isoformat(),
                proceeds_aud=q2(match.proceeds_aud),
                cost_aud=q2(match.cost_aud),
                gain_aud=q2(match.gain_aud),
                gain_ccy=q2(match.gain_ccy),
                currency=currency,
                held_over_12_months=match.held_over_12_months,
            )
            for match, currency, ticker in items
        ]
        groups.append(
            RealisedGroupView(
                financial_year=fy,
                total_aud=sum((row.gain_aud for row in view_rows), Decimal(0)),
                total_ccy=sum((row.gain_ccy for row in view_rows), Decimal(0)),
                rows=view_rows,
            )
        )
    return groups


def _banners(
    *,
    today: date,
    holdings: list[HoldingView],
    fx_quote: StoredFx | None,
    api_key: str | None,
    unpriced: int,
) -> list[str]:
    banners: list[str] = []
    if not api_key:
        banners.append(
            "Daily price updates are off. Set ALPHA_VANTAGE_API_KEY to enable "
            "them. Cost bases and realised gains still work."
        )
    stale_cutoff = today - timedelta(days=4)
    stale_prices = [
        holding.ticker
        for holding in holdings
        if holding.price_date is not None
        and date.fromisoformat(holding.price_date) < stale_cutoff
    ]
    if stale_prices:
        banners.append(
            "Prices look stale (older than 4 days) for "
            + ", ".join(stale_prices)
            + ". The last good close is still shown."
        )
    usd_open = any(holding.currency == "USD" for holding in holdings)
    if usd_open and fx_quote is None:
        banners.append(
            "No AUD/USD rate is stored yet, so USD holdings have no AUD value."
        )
    elif fx_quote is not None and date.fromisoformat(fx_quote.rate_date) < stale_cutoff:
        banners.append(
            f"AUD/USD {format_fx(fx_quote.rate)} is from {fx_quote.rate_date}, "
            "more than 4 days ago. The last good rate is still shown."
        )
    if unpriced:
        noun = "holding" if unpriced == 1 else "holdings"
        banners.append(
            f"{unpriced} {noun} ha{'s' if unpriced == 1 else 've'} no price yet "
            "and "
            f"{'is' if unpriced == 1 else 'are'} excluded from totals."
        )
    return banners
