"""Shares pages: trade entry, FIFO holdings, and a read-only JSON summary."""

import html
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.database import get_db
from app.middleware import get_current_user
from app.models import SharePrice, ShareTrade
from app.schemas import (
    SharesHoldingResponse,
    SharesSummaryResponse,
    ShareTradeCreate,
)
from app.services.shares import (
    QTY_QUANT,
    TICKER_RE,
    FxRateError,
    OversellError,
    TradeIn,
    build_shares_page,
    instrument_currency,
    load_trades,
    match_fifo,
    normalise_fx,
    normalise_ticker,
    parse_decimal,
    preview_message,
    q2,
    q_fx,
)
from app.templating import templates

router = APIRouter()


@dataclass
class ParsedTrade:
    ticker: str
    trade_type: str
    trade_date: date
    quantity: Decimal
    price: Decimal
    currency: str
    fee: Decimal
    fee_currency: str
    fx_rate_aud_usd: Decimal | None
    fx_rate_input: Decimal | None
    fx_input_convention: str | None
    notes: str | None

    def as_trade_in(self, trade_id: int) -> TradeIn:
        return TradeIn(
            id=trade_id,
            ticker=self.ticker,
            trade_type=self.trade_type,
            trade_date=self.trade_date,
            quantity=self.quantity,
            price=self.price,
            currency=self.currency,
            fee=self.fee,
            fee_currency=self.fee_currency,
            fx_aud_usd=self.fx_rate_aud_usd,
        )


def _error(message: str, *, status: int = 200) -> HTMLResponse:
    response = HTMLResponse(
        f'<p class="text-red-600 text-sm">{html.escape(message, quote=False)}</p>',
        status_code=status,
    )
    if status < 400:
        response.headers["HX-Retarget"] = "#shares-message"
        response.headers["HX-Reswap"] = "innerHTML"
    return response


def _parse_form(form: object) -> ParsedTrade:
    getter = getattr(form, "get", None)
    if not callable(getter):
        raise TypeError("Invalid form.")

    ticker = normalise_ticker(str(getter("ticker") or ""))
    if not ticker or TICKER_RE.fullmatch(ticker) is None:
        raise ValueError("Enter a ticker like AAPL or BHP.AX.")
    try:
        currency = instrument_currency(ticker)
    except ValueError as exc:
        raise ValueError(str(exc)) from None

    trade_type = str(getter("trade_type") or "").strip().lower()
    if trade_type not in {"buy", "sell"}:
        raise ValueError("Trade type must be buy or sell.")

    try:
        trade_date = date.fromisoformat(str(getter("trade_date") or ""))
    except ValueError:
        raise ValueError("Enter a valid trade date.") from None
    if trade_date > datetime.now(TIMEZONE).date():
        raise ValueError("Trade date cannot be in the future.")

    quantity = parse_decimal(getter("quantity"), "quantity")
    price = parse_decimal(getter("price"), "price")
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")
    if price <= 0:
        raise ValueError("Price must be greater than zero.")
    quantity = quantity.quantize(QTY_QUANT)
    price = price.quantize(QTY_QUANT)

    fee_raw = str(getter("fee") or "").strip()
    fee = Decimal(0) if fee_raw == "" else parse_decimal(fee_raw, "fee")
    if fee < 0:
        raise ValueError("Fee cannot be negative.")
    fee = q2(fee)

    fee_currency = str(getter("fee_currency") or "USD").strip().upper() or "USD"
    if fee_currency not in {"USD", "AUD"}:
        raise ValueError("Fee currency must be USD or AUD.")

    notes_raw = str(getter("notes") or "").strip()
    notes = notes_raw or None
    if notes is not None and len(notes) > 500:
        raise ValueError("Notes must be 500 characters or fewer.")

    fx_rate_aud: Decimal | None = None
    fx_input: Decimal | None = None
    convention: str | None = None
    if currency == "AUD":
        fee_currency = "AUD"
    else:
        fx_raw = str(getter("fx_rate") or "").strip()
        if not fx_raw:
            raise ValueError("An AUD/USD rate is required for US stocks.")
        convention = str(getter("fx_input_convention") or "AUD/USD").strip().upper()
        if convention not in {"AUD/USD", "USD/AUD"}:
            raise ValueError("FX convention must be AUD/USD or USD/AUD.")
        fx_input = parse_decimal(fx_raw, "FX rate")
        try:
            fx_rate_aud = normalise_fx(fx_input, convention)
        except FxRateError as exc:
            raise ValueError(str(exc)) from None
        fx_input = q_fx(fx_input)

    try:
        ShareTradeCreate.model_validate(
            {
                "ticker": ticker,
                "trade_type": trade_type,
                "trade_date": trade_date.isoformat(),
                "quantity": quantity,
                "price": price,
                "fee": fee,
                "fee_currency": fee_currency,
                "fx_rate": fx_input,
                "fx_input_convention": convention,
                "notes": notes,
            }
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = " ".join(str(part) for part in first.get("loc", ()))
        raise ValueError(f"Invalid {loc or 'trade'}.") from None

    return ParsedTrade(
        ticker=ticker,
        trade_type=trade_type,
        trade_date=trade_date,
        quantity=quantity,
        price=price,
        currency=currency,
        fee=fee,
        fee_currency=fee_currency,
        fx_rate_aud_usd=fx_rate_aud,
        fx_rate_input=fx_input,
        fx_input_convention=convention,
        notes=notes,
    )


def _next_id(db: Session) -> int:
    current = db.query(func.max(ShareTrade.id)).scalar()
    return int(current or 0) + 1


def _ensure_sequence(
    db: Session,
    parsed: ParsedTrade,
    *,
    trade_id: int,
    previous_ticker: str | None = None,
) -> None:
    tickers = {parsed.ticker}
    if previous_ticker is not None:
        tickers.add(previous_ticker)
    grouped = load_trades(db)
    for ticker in tickers:
        rows = [trade for trade in grouped.get(ticker, []) if trade.id != trade_id]
        if parsed.ticker == ticker:
            rows.append(parsed.as_trade_in(trade_id))
        if rows:
            match_fifo(rows)


def _apply(trade: ShareTrade, parsed: ParsedTrade) -> None:
    trade.ticker = parsed.ticker
    trade.trade_type = parsed.trade_type
    trade.trade_date = parsed.trade_date.isoformat()
    trade.quantity = parsed.quantity
    trade.price = parsed.price
    trade.currency = parsed.currency
    trade.fee = parsed.fee
    trade.fee_currency = parsed.fee_currency
    trade.fx_rate_aud_usd = parsed.fx_rate_aud_usd
    trade.fx_rate_input = parsed.fx_rate_input
    trade.fx_input_convention = parsed.fx_input_convention
    trade.notes = parsed.notes


def _results(request: Request, db: Session) -> HTMLResponse:
    page = build_shares_page(db)
    body = templates.TemplateResponse(
        request,
        "shares.html",
        {"page": page, "fragment": "holdings_and_realised"},
    ).body
    response = HTMLResponse(body)
    response.headers["HX-Trigger-After-Swap"] = "gwShareTradeSuccess"
    return response


def _page_context(request: Request, db: Session) -> dict[str, object]:
    user = get_current_user(request)
    page = build_shares_page(db)
    return {"username": user.username, "page": page}


@router.get("/shares", response_class=HTMLResponse)
async def shares_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "shares.html",
        _page_context(request, db),
    )


@router.get("/shares/fx-preview", response_class=HTMLResponse)
async def shares_fx_preview(request: Request):
    params = request.query_params
    ticker_raw = str(params.get("ticker") or "")
    if not ticker_raw.strip():
        return HTMLResponse("")
    try:
        ticker = normalise_ticker(ticker_raw)
        if TICKER_RE.fullmatch(ticker) is None:
            raise ValueError("Enter a ticker like AAPL or BHP.AX.")
        currency = instrument_currency(ticker)
        quantity_raw = str(params.get("quantity") or "").strip()
        price_raw = str(params.get("price") or "").strip()
        if not quantity_raw or not price_raw:
            return HTMLResponse("")
        quantity = parse_decimal(quantity_raw, "quantity")
        price = parse_decimal(price_raw, "price")
        fee_raw = str(params.get("fee") or "").strip()
        fee = Decimal(0) if fee_raw == "" else parse_decimal(fee_raw, "fee")
        fee_currency = str(params.get("fee_currency") or "USD").strip().upper() or "USD"
        if currency == "AUD":
            fee_currency = "AUD"
        fx_raw = str(params.get("fx_rate") or "").strip()
        convention = str(params.get("fx_input_convention") or "AUD/USD")
        message = preview_message(
            ticker=ticker,
            trade_type=str(params.get("trade_type") or "buy"),
            quantity=quantity,
            price=price,
            fee=fee,
            fee_currency=fee_currency,
            fx_rate=parse_decimal(fx_raw, "FX rate") if fx_raw else None,
            fx_convention=convention if fx_raw else None,
        )
    except (FxRateError, ValueError) as exc:
        return HTMLResponse(
            f'<p class="text-red-600 text-sm">{html.escape(str(exc))}</p>'
        )
    return HTMLResponse(
        f'<p class="gw-text-muted" style="font-size: 0.8rem;">{html.escape(message)}</p>'
    )


@router.post("/shares/trades", response_class=HTMLResponse)
async def shares_create_trade(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        parsed = _parse_form(form)
        _ensure_sequence(db, parsed, trade_id=_next_id(db))
    except (OversellError, ValueError, TypeError) as exc:
        return _error(str(exc))

    trade = ShareTrade(
        ticker=parsed.ticker,
        trade_type=parsed.trade_type,
        trade_date=parsed.trade_date.isoformat(),
        quantity=parsed.quantity,
        price=parsed.price,
        currency=parsed.currency,
        fee=parsed.fee,
        fee_currency=parsed.fee_currency,
        fx_rate_aud_usd=parsed.fx_rate_aud_usd,
        fx_rate_input=parsed.fx_rate_input,
        fx_input_convention=parsed.fx_input_convention,
        notes=parsed.notes,
    )
    db.add(trade)
    db.commit()
    return _results(request, db)


@router.get("/shares/trades/{trade_id}/edit", response_class=HTMLResponse)
async def shares_edit_form(
    request: Request, trade_id: int, db: Session = Depends(get_db)
):
    trade = db.get(ShareTrade, trade_id)
    if trade is None:
        return HTMLResponse("Not found", status_code=404)
    body = templates.TemplateResponse(
        request,
        "shares.html",
        {"trade": trade, "fragment": "trade_edit_row"},
    ).body
    return HTMLResponse(body)


@router.post("/shares/trades/{trade_id}", response_class=HTMLResponse)
async def shares_update_trade(
    request: Request, trade_id: int, db: Session = Depends(get_db)
):
    trade = db.get(ShareTrade, trade_id)
    if trade is None:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    try:
        parsed = _parse_form(form)
        _ensure_sequence(
            db,
            parsed,
            trade_id=trade.id,
            previous_ticker=trade.ticker,
        )
    except (OversellError, ValueError, TypeError) as exc:
        return _error(str(exc))
    _apply(trade, parsed)
    db.commit()
    return _results(request, db)


@router.delete("/shares/trades/{trade_id}", response_class=HTMLResponse)
async def shares_delete_trade(
    request: Request, trade_id: int, db: Session = Depends(get_db)
):
    trade = db.get(ShareTrade, trade_id)
    if trade is None:
        return HTMLResponse("Not found", status_code=404)
    remaining = [
        row for row in load_trades(db).get(trade.ticker, []) if row.id != trade.id
    ]
    try:
        if remaining:
            match_fifo(remaining)
    except OversellError as exc:
        when = (
            exc.trade_date.isoformat() if exc.trade_date is not None else "a later date"
        )
        return _error(
            f"Can't delete: sell on {when} would exceed holdings.",
            status=400,
        )
    db.delete(trade)
    db.commit()
    return _results(request, db)


@router.get("/shares/{ticker}", response_class=HTMLResponse)
async def shares_detail(request: Request, ticker: str, db: Session = Depends(get_db)):
    symbol = normalise_ticker(ticker)
    if TICKER_RE.fullmatch(symbol) is None:
        raise HTTPException(status_code=404, detail="Not found")
    trades = (
        db.query(ShareTrade)
        .filter(ShareTrade.ticker == symbol)
        .order_by(ShareTrade.trade_date.desc(), ShareTrade.id.desc())
        .all()
    )
    if not trades:
        raise HTTPException(status_code=404, detail="Not found")
    page = build_shares_page(db)
    holding = next((item for item in page.holdings if item.ticker == symbol), None)
    realised = [
        row
        for group in page.realised_groups
        for row in group.rows
        if row.ticker == symbol
    ]
    history = (
        db.query(SharePrice)
        .filter(SharePrice.ticker == symbol)
        .order_by(SharePrice.price_date.desc())
        .limit(30)
        .all()
    )
    user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "share_detail.html",
        {
            "username": user.username,
            "page": page,
            "ticker": symbol,
            "holding": holding,
            "realised": realised,
            "trades": trades,
            "history": history,
        },
    )


def _float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


@router.get("/api/shares")
async def shares_api(db: Session = Depends(get_db)):
    page = build_shares_page(db)
    summary = page.summary
    payload = SharesSummaryResponse(
        financial_year=summary.financial_year,
        cost_aud=float(summary.cost_aud),
        value_aud=float(summary.value_aud),
        value_usd=float(summary.value_usd),
        unrealised_aud=float(summary.unrealised_aud),
        unrealised_usd=float(summary.unrealised_usd),
        realised_aud=float(summary.realised_fy_aud),
        unpriced_count=summary.unpriced_count,
        fx_rate=float(page.fx.rate) if page.fx is not None else None,
        fx_date=page.fx.rate_date if page.fx is not None else None,
        fx_source=page.fx.source if page.fx is not None else None,
        price_as_of=page.price_as_of,
        holdings=[
            SharesHoldingResponse(
                ticker=holding.ticker,
                quantity=float(holding.quantity),
                currency=holding.currency,
                cost_aud=float(holding.cost_aud),
                value_usd=_float(holding.value_usd),
                value_aud=_float(holding.value_aud),
                unrealised_aud=_float(holding.unrealised_aud),
                unrealised_usd=_float(holding.unrealised_usd),
                pct=_float(holding.pct),
                price=_float(holding.price),
                price_date=holding.price_date,
            )
            for holding in page.holdings
        ],
        realised_by_fy={
            group.financial_year: float(group.total_aud)
            for group in page.realised_groups
        },
    )
    body = payload.model_dump()
    body["as_of"] = {
        "price_date": page.price_as_of,
        "fx_rate": body["fx_rate"],
        "fx_date": body["fx_date"],
        "fx_source": body["fx_source"],
        "updated": page.price_fetched_local,
    }
    return JSONResponse(body)
