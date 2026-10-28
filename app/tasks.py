"""Background tasks for automated income allocation and bill processing."""

import calendar
import logging
import threading
import traceback
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
from sqlalchemy.orm import Session

import app.config as app_config
from app.config import TIMEZONE
from app.database import SessionLocal
from app.models import (
    Budget,
    Category,
    FxRate,
    IncomeAllocation,
    MarketDataRun,
    MonthlyUnallocatedIncome,
    RecurringBill,
    SharePrice,
    ShareTrade,
    SinkingFund,
    Transaction,
)
from app.services.market_data import FxProvider, PriceProvider, RequestGate

logger = logging.getLogger(__name__)

FREQUENCY_ANNUAL_MULTIPLIER = {
    "monthly": 12,
    "quarterly": 4,
    "yearly": 1,
    "28_days": Decimal("13.036"),
}


def _today() -> date:
    """Return today's date in the configured timezone."""
    return datetime.now(TIMEZONE).date()


def advance_due_date(current: date, frequency: str) -> date:
    """Advance a due date based on frequency, clamping to last day of month."""
    if frequency == "28_days":
        return current + timedelta(days=28)

    if frequency == "monthly":
        month = current.month + 1
        year = current.year
        if month > 12:
            month = 1
            year += 1
        day = min(current.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    if frequency == "quarterly":
        month = current.month + 3
        year = current.year
        while month > 12:
            month -= 12
            year += 1
        day = min(current.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    if frequency == "yearly":
        year = current.year + 1
        day = min(current.day, calendar.monthrange(year, current.month)[1])
        return date(year, current.month, day)

    return current + timedelta(days=30)


def _compute_bills_recommended(db: Session) -> Decimal:
    """Calculate recommended monthly bills allocation: total annual cost / 12."""
    bills = db.query(RecurringBill).filter(RecurringBill.is_active == True).all()
    total_annual = sum(
        (
            Decimal(str(b.amount))
            * Decimal(str(FREQUENCY_ANNUAL_MULTIPLIER.get(b.frequency, 1)))
            for b in bills
        ),
        Decimal(0),
    )
    if total_annual == 0:
        return Decimal(0)
    return (total_annual / 12).quantize(Decimal("0.01"))


def generate_bills_forecast(db: Session, months: int = 12) -> list[dict]:
    """Project Bills fund cash flow over the next N months.

    Returns a list of month dicts (length == months) with keys:
        month, year, month_name, bills, total_out, contribution, closing_balance
    """
    today = _today()

    active_bills = db.query(RecurringBill).filter(RecurringBill.is_active == True).all()

    bills_fund = (
        db.query(SinkingFund)
        .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)
        .first()
    )
    running_balance = (
        Decimal(str(bills_fund.current_balance)) if bills_fund else Decimal(0)
    )

    # Determine monthly contribution from income allocation config
    allocation = db.query(IncomeAllocation).first()
    if allocation:
        if allocation.bills_fund_allocation_type == "fixed":
            monthly_contribution = Decimal(str(allocation.bills_fund_fixed_amount or 0))
        else:
            monthly_contribution = _compute_bills_recommended(db)
    else:
        monthly_contribution = Decimal(0)

    # Check if this month's contribution has already been deposited
    month_start = f"{today.year}-{today.month:02d}-01"
    month_end = f"{today.year}-{today.month:02d}-{calendar.monthrange(today.year, today.month)[1]:02d}"
    already_contributed = False
    if bills_fund:
        already_contributed = (
            db.query(Transaction)
            .filter(
                Transaction.transaction_type == "income_allocation",
                Transaction.sinking_fund_id == bills_fund.id,
                Transaction.date >= month_start,
                Transaction.date <= month_end,
            )
            .first()
        ) is not None

    # Calculate the horizon end date
    end_abs = today.month - 1 + months - 1
    horizon_end = date(
        today.year + end_abs // 12,
        end_abs % 12 + 1,
        calendar.monthrange(today.year + end_abs // 12, end_abs % 12 + 1)[1],
    )

    # Project each bill's payment dates forward into monthly buckets
    monthly_bills: dict[tuple[int, int], list[dict]] = {}
    for bill in active_bills:
        current_due = date.fromisoformat(bill.next_due_date)
        while current_due <= horizon_end:
            ym = (current_due.year, current_due.month)
            monthly_bills.setdefault(ym, []).append(
                {
                    "id": bill.id,
                    "name": bill.name,
                    "amount": Decimal(str(bill.amount)),
                    "bill_type": bill.bill_type,
                    "due_date": current_due,
                    "foreign_amount": bill.foreign_amount,
                    "foreign_currency": bill.foreign_currency,
                }
            )
            current_due = advance_due_date(current_due, bill.frequency)

    # Build forecast rows
    result = []
    for i in range(months):
        abs_month = today.month - 1 + i
        m = abs_month % 12 + 1
        y = today.year + abs_month // 12

        bills_this_month = monthly_bills.get((y, m), [])
        total_out = sum((b["amount"] for b in bills_this_month), Decimal(0))

        # Skip contribution for current month if already processed
        contribution = (
            Decimal(0) if (i == 0 and already_contributed) else monthly_contribution
        )

        running_balance = running_balance + contribution - total_out

        result.append(
            {
                "month": m,
                "year": y,
                "month_name": calendar.month_name[m],
                "bills": bills_this_month,
                "total_out": total_out.quantize(Decimal("0.01")),
                "contribution": contribution.quantize(Decimal("0.01")),
                "closing_balance": running_balance.quantize(Decimal("0.01")),
            }
        )

    return result


def process_income_allocation(db: Session | None = None) -> None:
    """Monthly task (1st): distribute income per IncomeAllocation config.

    Args:
        db: Optional database session for testing. If None, creates one
            from SessionLocal and manages its lifecycle.
    """
    _managed = db is None
    if _managed:
        db = SessionLocal()
    assert db is not None
    try:
        today = _today()
        month, year = today.month, today.year
        date_str = today.isoformat()
        month_start = f"{year}-{month:02d}-01"
        month_end = f"{year}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"

        # Idempotency: skip if income already processed this month
        existing = (
            db.query(Transaction)
            .filter(
                Transaction.transaction_type == "income",
                Transaction.date >= month_start,
                Transaction.date <= month_end,
            )
            .first()
        )
        if existing:
            logger.info("Income already processed for %s-%02d, skipping", year, month)
            return

        allocation = db.query(IncomeAllocation).first()
        if not allocation:
            logger.warning("No income allocation config found, skipping")
            return

        income_amount = Decimal(str(allocation.monthly_income_amount))
        budget_amount = Decimal(str(allocation.monthly_budget_allocation))

        # Find required categories
        income_cat = (
            db.query(Category)
            .filter(Category.type == "income", Category.is_deleted == False)
            .first()
        )
        if not income_cat:
            logger.warning("No income category found, skipping income processing")
            return

        transfer_cat = (
            db.query(Category)
            .filter(Category.type == "transfer", Category.is_deleted == False)
            .first()
        )
        if not transfer_cat:
            logger.warning("No Transfer category found, skipping income processing")
            return

        # 1. Create income transaction
        db.add(
            Transaction(
                date=date_str,
                description=f"Monthly income \u2014 {today.strftime('%B %Y')}",
                amount=income_amount,
                category_id=income_cat.id,
                type="income",
                transaction_type="income",
            )
        )

        total_allocated = Decimal(0)

        # Identify the Bills fund (handled separately)
        bills_fund = (
            db.query(SinkingFund)
            .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)
            .first()
        )
        bills_fund_id = bills_fund.id if bills_fund else None

        # 2. Distribute to sinking funds per junction table (skip Bills)
        for junction in allocation.sinking_fund_allocations:
            if junction.sinking_fund_id == bills_fund_id:
                continue

            fund = (
                db.query(SinkingFund)
                .filter(
                    SinkingFund.id == junction.sinking_fund_id,
                    SinkingFund.is_deleted == False,
                )
                .first()
            )
            if not fund:
                continue

            amount = Decimal(str(junction.allocation_amount))
            if amount <= 0:
                continue

            db.add(
                Transaction(
                    date=date_str,
                    description=f"Income allocation to {fund.name}",
                    amount=amount,
                    category_id=transfer_cat.id,
                    type="transfer",
                    transaction_type="income_allocation",
                    sinking_fund_id=fund.id,
                )
            )
            fund.current_balance = float(Decimal(str(fund.current_balance)) + amount)
            total_allocated += amount

        # 3. Handle Bills fund allocation
        if bills_fund:
            if allocation.bills_fund_allocation_type == "fixed":
                bills_amount = Decimal(str(allocation.bills_fund_fixed_amount or 0))
            else:
                bills_amount = _compute_bills_recommended(db)

            if bills_amount > 0:
                db.add(
                    Transaction(
                        date=date_str,
                        description="Income allocation to Bills fund",
                        amount=bills_amount,
                        category_id=transfer_cat.id,
                        type="transfer",
                        transaction_type="income_allocation",
                        sinking_fund_id=bills_fund.id,
                    )
                )
                bills_fund.current_balance = float(
                    Decimal(str(bills_fund.current_balance)) + bills_amount
                )
                total_allocated += bills_amount

        # 3b. Recurring transfers (money that leaves the household budget entirely)
        month_name = today.strftime("%B")
        for transfer in allocation.recurring_transfers:
            transfer_amount = Decimal(str(transfer.amount))
            db.add(
                Transaction(
                    date=date_str,
                    description=f"{transfer.description} \u2014 {month_name} {year}",
                    amount=float(transfer_amount),
                    category_id=transfer_cat.id,
                    type="expense",
                    transaction_type="income_allocation",
                )
            )
            total_allocated += transfer_amount

        # 3c. Sweep prior-month budget surplus to overflow sinking fund
        prev_month = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1

        overflow_fund = None
        if allocation.overflow_sinking_fund_id:
            overflow_fund = (
                db.query(SinkingFund)
                .filter(
                    SinkingFund.id == allocation.overflow_sinking_fund_id,
                    SinkingFund.is_deleted == False,
                )
                .first()
            )

        if overflow_fund:
            prev_budgets = (
                db.query(Budget)
                .filter(Budget.month == prev_month, Budget.year == prev_year)
                .all()
            )
            total_surplus = Decimal(0)
            for pb in prev_budgets:
                surplus = (
                    Decimal(str(pb.allocated_amount))
                    - Decimal(str(pb.spent_amount))
                    + Decimal(str(pb.fund_balance))
                )
                total_surplus += surplus

            prev_month_name = calendar.month_name[prev_month]
            if total_surplus > 0:
                db.add(
                    Transaction(
                        date=date_str,
                        description=f"Budget surplus sweep \u2014 {prev_month_name} {prev_year}",
                        amount=float(total_surplus),
                        category_id=transfer_cat.id,
                        type="transfer",
                        transaction_type="contribution",
                        sinking_fund_id=overflow_fund.id,
                    )
                )
                overflow_fund.current_balance = float(
                    Decimal(str(overflow_fund.current_balance)) + total_surplus
                )
            elif total_surplus < 0:
                shortfall = abs(total_surplus)
                db.add(
                    Transaction(
                        date=date_str,
                        description=f"Budget shortfall \u2014 {prev_month_name} {prev_year}",
                        amount=float(shortfall),
                        category_id=transfer_cat.id,
                        type="expense",
                        transaction_type="withdrawal",
                        sinking_fund_id=overflow_fund.id,
                    )
                )
                overflow_fund.current_balance = float(
                    Decimal(str(overflow_fund.current_balance)) - shortfall
                )

        # 4. Ensure Budget rows exist for this month

        budget_cats = (
            db.query(Category)
            .filter(
                Category.is_budget_category == True,
                Category.is_deleted == False,
            )
            .all()
        )
        for cat in budget_cats:
            existing_budget = (
                db.query(Budget)
                .filter(
                    Budget.category_id == cat.id,
                    Budget.month == month,
                    Budget.year == year,
                )
                .first()
            )
            if not existing_budget:
                prev_budget = (
                    db.query(Budget)
                    .filter(
                        Budget.category_id == cat.id,
                        Budget.month == prev_month,
                        Budget.year == prev_year,
                    )
                    .first()
                )
                allocated = prev_budget.allocated_amount if prev_budget else 0
                db.add(
                    Budget(
                        category_id=cat.id,
                        month=month,
                        year=year,
                        allocated_amount=allocated,
                        spent_amount=0,
                        fund_balance=0,
                    )
                )

        total_allocated += budget_amount

        # 5. Record unallocated income
        unallocated = income_amount - total_allocated
        existing_unalloc = (
            db.query(MonthlyUnallocatedIncome)
            .filter(
                MonthlyUnallocatedIncome.month == month,
                MonthlyUnallocatedIncome.year == year,
            )
            .first()
        )
        if existing_unalloc:
            existing_unalloc.unallocated_amount = float(unallocated)
        else:
            db.add(
                MonthlyUnallocatedIncome(
                    month=month,
                    year=year,
                    unallocated_amount=unallocated,
                )
            )

        db.commit()
        logger.info(
            "Income allocation completed for %s-%02d: "
            "income=%s, allocated=%s, unallocated=%s",
            year,
            month,
            income_amount,
            total_allocated,
            unallocated,
        )

    except Exception:
        db.rollback()
        logger.exception("Error processing income allocation")
        raise
    finally:
        if _managed:
            db.close()


def process_due_bills(db: Session | None = None) -> None:
    """Daily task: process bills due today or overdue.

    Args:
        db: Optional database session for testing. If None, creates one
            from SessionLocal and manages its lifecycle.
    """
    _managed = db is None
    if _managed:
        db = SessionLocal()
    assert db is not None
    try:
        today = _today()
        today_str = today.isoformat()

        bills_fund = (
            db.query(SinkingFund)
            .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)
            .first()
        )
        if not bills_fund:
            logger.warning("No Bills sinking fund found, skipping bill processing")
            return

        due_bills = (
            db.query(RecurringBill)
            .filter(
                RecurringBill.is_active == True,
                RecurringBill.next_due_date <= today_str,
            )
            .all()
        )

        processed = 0
        for bill in due_bills:
            # Skip variable bills — they require manual payment
            if getattr(bill, "bill_type", "fixed") == "variable":
                continue

            # Idempotency: skip if already paid today for this bill
            existing = (
                db.query(Transaction)
                .filter(
                    Transaction.recurring_bill_id == bill.id,
                    Transaction.date == today_str,
                )
                .first()
            )
            if existing:
                continue

            amount = Decimal(str(bill.amount))

            # Dual-linkage transaction: sinking_fund_id + recurring_bill_id
            db.add(
                Transaction(
                    date=today_str,
                    description=f"Auto-payment: {bill.name} to {bill.debtor_provider}",
                    amount=amount,
                    category_id=bill.category_id,
                    type="expense",
                    transaction_type="regular",
                    sinking_fund_id=bills_fund.id,
                    recurring_bill_id=bill.id,
                )
            )

            bills_fund.current_balance = float(
                Decimal(str(bills_fund.current_balance)) - amount
            )

            # Advance next_due_date
            current_due = date.fromisoformat(bill.next_due_date)
            bill.next_due_date = advance_due_date(
                current_due, bill.frequency
            ).isoformat()
            processed += 1

        db.commit()
        if processed:
            logger.info("Processed %d due bill(s) for %s", processed, today_str)

    except Exception:
        db.rollback()
        logger.exception("Error processing due bills")
        raise
    finally:
        if _managed:
            db.close()


# ---------------------------------------------------------------------------
# Share prices and AUD/USD
# ---------------------------------------------------------------------------

_market_data_stop = threading.Event()


class _Unset:
    """Sentinel: build the default price provider instead of using the argument."""


_UNSET = _Unset()


def market_data_sleep(seconds: float) -> None:
    """Sleep that returns early when the scheduler is stopping."""
    _market_data_stop.wait(timeout=seconds)


def request_market_data_stop() -> None:
    _market_data_stop.set()


def clear_market_data_stop() -> None:
    _market_data_stop.clear()


def market_data_stop_requested() -> bool:
    return _market_data_stop.is_set()


def _aware_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _sweep_market_data_runs(db: Session) -> None:
    now = datetime.now(UTC)
    stale_before = now - timedelta(hours=1)
    for row in db.query(MarketDataRun).filter(MarketDataRun.status == "running").all():
        if _aware_utc(row.started_at) < stale_before:
            row.status = "failed"
            row.finished_at = now
            row.message = "Run did not finish (treated as failed after 1 hour)."
    keep_after = now - timedelta(days=90)
    for row in db.query(MarketDataRun).all():
        if _aware_utc(row.started_at) < keep_after:
            db.delete(row)
    db.commit()


def _upsert_price(
    db: Session, ticker: str, price_date: str, close: Decimal, currency: str
) -> None:
    row = (
        db.query(SharePrice)
        .filter(SharePrice.ticker == ticker, SharePrice.price_date == price_date)
        .one_or_none()
    )
    now = datetime.now(UTC)
    if row is None:
        db.add(
            SharePrice(
                ticker=ticker,
                price_date=price_date,
                close=close,
                currency=currency,
                source="alphavantage",
                fetched_at=now,
            )
        )
        return
    if row.close != close or row.source != "alphavantage":
        row.close = close
        row.currency = currency
        row.source = "alphavantage"
        row.fetched_at = now


def _upsert_fx(db: Session, obs: object) -> None:
    from app.services.market_data import FxObservation

    if not isinstance(obs, FxObservation):
        raise TypeError("Expected an FxObservation.")
    row = (
        db.query(FxRate)
        .filter(
            FxRate.base == obs.base,
            FxRate.quote == obs.quote,
            FxRate.rate_date == obs.rate_date.isoformat(),
        )
        .one_or_none()
    )
    now = datetime.now(UTC)
    if row is None:
        db.add(
            FxRate(
                base=obs.base,
                quote=obs.quote,
                rate_date=obs.rate_date.isoformat(),
                rate=obs.rate,
                source=obs.source,
                fetched_at=now,
            )
        )
        return
    # Alpha Vantage wins over Frankfurter for the same date, never the reverse.
    if row.source == "alphavantage" and obs.source != "alphavantage":
        return
    if row.rate != obs.rate or row.source != obs.source:
        row.rate = obs.rate
        row.source = obs.source
        row.fetched_at = now


def _stalest_first(db: Session, tickers: list[str]) -> list[str]:
    def sort_key(ticker: str) -> tuple[int, str]:
        row = (
            db.query(SharePrice)
            .filter(SharePrice.ticker == ticker)
            .order_by(SharePrice.price_date.desc())
            .first()
        )
        if row is None:
            return (0, "")
        return (1, row.price_date)

    return sorted(tickers, key=sort_key)


def _catchup_due(db: Session) -> bool:
    latest = (
        db.query(MarketDataRun)
        .order_by(MarketDataRun.started_at.desc(), MarketDataRun.id.desc())
        .first()
    )
    if latest is not None:
        age = datetime.now(UTC) - _aware_utc(latest.started_at)
        if age < timedelta(hours=20):
            return False
    from app.services.shares import held_tickers

    held = held_tickers(db)
    fx_exists = (
        db.query(FxRate).filter(FxRate.base == "AUD", FxRate.quote == "USD").first()
        is not None
    )
    return bool(held) or not fx_exists


def process_share_market_data_if_stale(
    db: Session | None = None,
    *,
    price_provider: PriceProvider | None | _Unset = _UNSET,
    fx_providers: list[FxProvider] | None = None,
    gate: RequestGate | None = None,
) -> None:
    """Startup catch-up. Skips when a run started within the last 20 hours."""
    managed = db is None
    if db is None:
        db = SessionLocal()
    assert db is not None
    try:
        if not _catchup_due(db):
            return
        process_share_market_data(
            db,
            price_provider=price_provider,
            fx_providers=fx_providers,
            gate=gate,
        )
    finally:
        if managed:
            db.close()


def process_share_market_data(
    db: Session | None = None,
    *,
    price_provider: PriceProvider | None | _Unset = _UNSET,
    fx_providers: list[FxProvider] | None = None,
    gate: RequestGate | None = None,
) -> None:
    """Fetch AUD/USD and daily closes for open holdings.

    Frankfurter is the primary FX source. Alpha Vantage prices are skipped
    when the key is missing or when Alpha Vantage reports a rate limit.
    Failures keep the last good stored price. The error is not re-raised.
    """
    from app.services.market_data import (
        BudgetExhaustedError,
        MarketDataError,
        ProviderResponseError,
        RateLimitedError,
        SymbolNotFoundError,
        build_fx_providers,
        build_price_provider,
        make_client,
        redact,
    )
    from app.services.shares import held_tickers

    managed = db is None
    if db is None:
        db = SessionLocal()
    assert db is not None
    client: httpx.Client | None = None
    run_id: int | None = None
    tickers_updated = 0
    active_gate = gate
    try:
        _sweep_market_data_runs(db)
        run = MarketDataRun(
            status="running",
            started_at=datetime.now(UTC),
            av_requests=0,
            tickers_updated=0,
        )
        db.add(run)
        db.commit()
        run_id = run.id

        trade_count = db.query(ShareTrade).count()
        held = held_tickers(db) if trade_count else []
        fx_exists = (
            db.query(FxRate).filter(FxRate.base == "AUD", FxRate.quote == "USD").first()
            is not None
        )
        if trade_count == 0 or (not held and fx_exists):
            run.status = "skipped"
            run.finished_at = datetime.now(UTC)
            run.message = (
                "No trades."
                if trade_count == 0
                else "No open holdings; FX rate already stored."
            )
            db.commit()
            return

        if active_gate is None:
            active_gate = RequestGate(
                max_requests=app_config.SHARES_AV_MAX_REQUESTS_PER_RUN,
                min_interval_s=app_config.SHARES_AV_MIN_INTERVAL_SECONDS,
                sleep=market_data_sleep,
            )
        if isinstance(price_provider, _Unset) or fx_providers is None:
            client = make_client()
        resolved_prices: PriceProvider | None
        if isinstance(price_provider, _Unset):
            assert client is not None
            resolved_prices = build_price_provider(client, active_gate)
        else:
            resolved_prices = price_provider
        if fx_providers is None:
            assert client is not None
            resolved_fx = build_fx_providers(client, active_gate)
        else:
            resolved_fx = fx_providers

        fx_source: str | None = None
        av_blocked = False
        problems: list[str] = []
        skipped: list[str] = []

        for provider in resolved_fx:
            name = getattr(provider, "name", "fx")
            try:
                observations = provider.aud_usd()
            except RateLimitedError as exc:
                av_blocked = True
                problems.append(redact(str(exc))[:200])
                logger.warning(
                    "FX provider %s rate limited: %s", name, redact(str(exc))
                )
                continue
            except (ProviderResponseError, MarketDataError) as exc:
                problems.append(f"{name} failed")
                logger.warning("FX provider %s failed: %s", name, redact(str(exc)))
                continue
            for observation in list(observations)[:10]:
                _upsert_fx(db, observation)
            db.commit()
            fx_source = str(name)
            break

        if held and resolved_prices is None:
            if app_config.ALPHA_VANTAGE_API_KEY:
                problems.append("Share prices were not fetched.")
            else:
                logger.warning("ALPHA_VANTAGE_API_KEY not set; skipping share prices")
                problems.append("ALPHA_VANTAGE_API_KEY not set; skipping share prices")
        elif resolved_prices is not None and held and not av_blocked:
            ordered = _stalest_first(db, held)
            for index, ticker in enumerate(ordered):
                if market_data_stop_requested():
                    skipped = ordered[index:]
                    problems.append("Stopped early.")
                    break
                if not resolved_prices.supports(ticker):
                    problems.append(f"{ticker} is not supported for pricing")
                    continue
                try:
                    closes = resolved_prices.daily_closes(ticker)
                except BudgetExhaustedError:
                    skipped = ordered[index:]
                    logger.warning(
                        "Alpha Vantage budget exhausted; skipped %s",
                        ", ".join(skipped),
                    )
                    break
                except RateLimitedError as exc:
                    av_blocked = True
                    skipped = ordered[index:]
                    problems.append(redact(str(exc))[:200])
                    logger.warning(
                        "Alpha Vantage rate limited; skipped %s",
                        ", ".join(skipped),
                    )
                    break
                except SymbolNotFoundError:
                    logger.warning("No price for %s (symbol not found)", ticker)
                    problems.append(f"No price for {ticker}")
                    continue
                except ProviderResponseError as exc:
                    logger.warning(
                        "Price fetch failed for %s: %s", ticker, redact(str(exc))
                    )
                    problems.append(f"Price fetch failed for {ticker}")
                    continue
                existing = (
                    db.query(SharePrice).filter(SharePrice.ticker == ticker).first()
                )
                limit = (
                    len(closes)
                    if existing is None
                    else app_config.SHARES_PRICE_UPSERT_DAYS
                )
                for close in list(closes)[:limit]:
                    _upsert_price(
                        db,
                        close.ticker,
                        close.price_date.isoformat(),
                        close.close,
                        close.currency,
                    )
                db.commit()
                tickers_updated += 1

        prices_complete = not held or (
            resolved_prices is not None
            and not av_blocked
            and not skipped
            and tickers_updated == len(held)
        )
        if fx_source and prices_complete:
            status = "success"
        elif fx_source or tickers_updated:
            status = "partial"
        else:
            status = "failed"

        parts: list[str] = []
        if fx_source:
            parts.append(f"FX from {fx_source}")
        else:
            parts.append("FX unavailable")
        parts.append(f"prices updated {tickers_updated}/{len(held)}")
        if skipped:
            parts.append("skipped " + ", ".join(skipped))
        if problems:
            parts.append("; ".join(problems))
        message = redact(" — ".join(parts))[:1000]

        run.status = status
        run.finished_at = datetime.now(UTC)
        run.av_requests = active_gate.used
        run.tickers_updated = tickers_updated
        run.fx_source = fx_source
        run.message = message
        db.commit()
    except Exception as exc:  # noqa: BLE001 — the job must not re-raise
        db.rollback()
        from app.services.market_data import redact

        safe = redact(traceback.format_exc())
        logger.error("Share market data run failed\n%s", safe)
        if run_id is not None:
            failed = db.get(MarketDataRun, run_id)
            if failed is not None:
                failed.status = "failed"
                failed.finished_at = datetime.now(UTC)
                failed.message = redact(str(exc))[:1000]
                failed.tickers_updated = tickers_updated
                if active_gate is not None:
                    failed.av_requests = active_gate.used
                db.commit()
    finally:
        if client is not None:
            client.close()
        if managed:
            db.close()
