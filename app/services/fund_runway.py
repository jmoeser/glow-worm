"""How long a sinking fund's balance lasts at its recent net rate.

Computed on request for the fund detail page. No tables, migrations, or settings.
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, ROUND_UP, Decimal
from typing import Literal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.models import SinkingFund, Transaction
from app.services.money import add_months, dollars, money

logger = logging.getLogger(__name__)

WINDOW_MONTHS = 6
MIN_MONTHS = 2
WARN_BELOW_MONTHS = 6
CAP_MONTHS = 120

_ZERO = Decimal("0.00")
_TENTH = Decimal("0.1")

RunwayState = Literal[
    "running_down",
    "empty",
    "growing",
    "flat",
    "no_activity",
    "payoff",
    "debt_stuck",
    "too_new",
    "bills",
    "hidden",
]


def balance_direction(txn_type: str) -> int:
    """+1 for income/transfer, -1 otherwise. Mirrors _adjust_sinking_fund_balance."""
    return 1 if txn_type in ("income", "transfer") else -1


def _aware_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _months_word(count: int) -> str:
    return "month" if count == 1 else "months"


def _month_year(day: date) -> str:
    return f"{calendar.month_name[day.month]} {day.year}"


def _long_span(cap_months: int) -> str:
    if cap_months % 12 == 0:
        years = cap_months // 12
        unit = "year" if years == 1 else "years"
        return f"more than {years} {unit}"
    return f"more than {cap_months} {_months_word(cap_months)}"


def _shift_months(today: date, months: Decimal) -> date:
    days = int(months * Decimal("365.25") / 12)
    return today + timedelta(days=days)


def _first_full_month(history_start: date) -> date:
    if history_start.day == 1:
        return history_start
    return add_months(date(history_start.year, history_start.month, 1), 1)


def _count_months(start: date, end: date) -> int:
    if start > end:
        return 0
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _parse_date(value: str) -> date | None:
    if len(value) == 10:
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    logger.debug("Ignoring unparseable transaction date %r", value)
    return None


def _earliest_txn_date(db: Session, fund_id: int) -> date | None:
    raw = (
        db.query(func.min(Transaction.date))
        .filter(Transaction.sinking_fund_id == fund_id)
        .scalar()
    )
    if not isinstance(raw, str):
        return None
    return _parse_date(raw)


def _window_totals(
    db: Session, fund_id: int, start: date, end: date
) -> tuple[Decimal, Decimal]:
    rows = (
        db.query(Transaction.type, Transaction.amount)
        .filter(
            Transaction.sinking_fund_id == fund_id,
            Transaction.date >= start.isoformat(),
            Transaction.date <= end.isoformat(),
        )
        .all()
    )
    total_in = Decimal(0)
    total_out = Decimal(0)
    for txn_type, amount in rows:
        signed = Decimal(str(amount))
        if balance_direction(txn_type) > 0:
            total_in += signed
        else:
            total_out += signed
    return money(total_in), money(total_out)


@dataclass(frozen=True)
class FundRunwayResult:
    state: RunwayState
    months_used: int
    window_start: date | None
    window_end: date | None
    total_in: Decimal
    total_out: Decimal
    avg_in: Decimal
    avg_out: Decimal
    net_burn: Decimal
    balance: Decimal
    runway_months: Decimal | None
    run_out_date: date | None
    months_to_zero: Decimal | None
    back_to_zero_date: date | None
    is_capped: bool
    is_warning: bool
    # Kept so a test override of the constants still produces the right sentence.
    window_months: int = WINDOW_MONTHS
    min_months: int = MIN_MONTHS
    cap_months: int = CAP_MONTHS

    @property
    def net_inflow(self) -> Decimal:
        return -self.net_burn

    @property
    def headline(self) -> str:
        if self.state == "hidden":
            return ""
        if self.state == "bills":
            return (
                "Bills come in lumps, so runway isn't shown here. "
                "See Forecast for what's ahead."
            )
        if self.state == "too_new":
            return (
                "Runway shows once this fund has "
                f"{self.min_months} full months of history."
            )
        if self.state == "payoff":
            return self._payoff_headline()
        if self.state == "debt_stuck":
            return (
                f"This fund is {dollars(abs(self.balance))} in the red "
                "and isn't being paid down at the moment."
            )
        if self.state == "no_activity":
            return f"No money in or out over the last {self._span()}."
        if self.state == "growing":
            return (
                "This fund is growing: contributions have covered spending "
                f"over the last {self._span()}."
            )
        if self.state == "flat":
            return f"Money in has matched money out over the last {self._span()}."
        if self.state == "empty":
            return "This fund is empty and more is going out than coming in."
        return self._running_down_headline()

    @property
    def detail(self) -> str | None:
        if self.state in {"flat", "no_activity", "too_new", "bills", "hidden"}:
            return None
        if self.state == "debt_stuck" and self.total_in == 0 and self.total_out == 0:
            return None
        if self.state not in {
            "running_down",
            "empty",
            "growing",
            "payoff",
            "debt_stuck",
        }:
            return None
        line = (
            f"Last {self.months_used} {_months_word(self.months_used)}: "
            f"{dollars(self.avg_in)} in, {dollars(self.avg_out)} out a month "
            "on average."
        )
        if self.min_months <= self.months_used < self.window_months:
            line += f" (fund is newer than {self.window_months} months)"
        return line

    def _span(self) -> str:
        return f"{self.months_used} {_months_word(self.months_used)}"

    def _running_down_headline(self) -> str:
        if self.is_capped:
            return f"At recent spending, this fund lasts {_long_span(self.cap_months)}."
        runway_months = self.runway_months or Decimal(0)
        if runway_months < 1 or self.run_out_date is None:
            return "At recent spending, this fund runs out in less than a month."
        whole = int(runway_months.to_integral_value(rounding=ROUND_FLOOR))
        return (
            "At recent spending, this fund runs out in about "
            f"{whole} {_months_word(whole)} (around {_month_year(self.run_out_date)})."
        )

    def _payoff_headline(self) -> str:
        if self.is_capped:
            return (
                "At recent repayments, this fund is back to zero in "
                f"{_long_span(self.cap_months)}."
            )
        months_to_zero = self.months_to_zero or Decimal(0)
        whole = max(int(months_to_zero.to_integral_value(rounding=ROUND_CEILING)), 1)
        if self.back_to_zero_date is None:
            return (
                "At recent repayments, this fund is back to zero in "
                f"about {whole} {_months_word(whole)}."
            )
        return (
            "At recent repayments, this fund is back to zero in about "
            f"{whole} {_months_word(whole)} "
            f"(around {_month_year(self.back_to_zero_date)})."
        )


def _result(
    state: RunwayState,
    *,
    balance: Decimal,
    months_used: int = 0,
    window_start: date | None = None,
    window_end: date | None = None,
    total_in: Decimal = _ZERO,
    total_out: Decimal = _ZERO,
    avg_in: Decimal = _ZERO,
    avg_out: Decimal = _ZERO,
    net_burn: Decimal = _ZERO,
    runway_months: Decimal | None = None,
    run_out_date: date | None = None,
    months_to_zero: Decimal | None = None,
    back_to_zero_date: date | None = None,
    is_capped: bool = False,
    is_warning: bool = False,
    window_months: int = WINDOW_MONTHS,
    min_months: int = MIN_MONTHS,
    cap_months: int = CAP_MONTHS,
) -> FundRunwayResult:
    return FundRunwayResult(
        state=state,
        months_used=months_used,
        window_start=window_start,
        window_end=window_end,
        total_in=total_in,
        total_out=total_out,
        avg_in=avg_in,
        avg_out=avg_out,
        net_burn=net_burn,
        balance=balance,
        runway_months=runway_months,
        run_out_date=run_out_date,
        months_to_zero=months_to_zero,
        back_to_zero_date=back_to_zero_date,
        is_capped=is_capped,
        is_warning=is_warning,
        window_months=window_months,
        min_months=min_months,
        cap_months=cap_months,
    )


def compute_fund_runway(
    db: Session,
    fund: SinkingFund,
    *,
    today: date | None = None,
    window_months: int = WINDOW_MONTHS,
    min_months: int = MIN_MONTHS,
    warn_below_months: int = WARN_BELOW_MONTHS,
    cap_months: int = CAP_MONTHS,
) -> FundRunwayResult:
    """Runway or debt payoff for one fund, as of ``today``."""
    as_of = today or datetime.now(TIMEZONE).date()
    balance = money(fund.current_balance)
    limits = {
        "window_months": window_months,
        "min_months": min_months,
        "cap_months": cap_months,
    }
    if fund.is_deleted:
        return _result("hidden", balance=balance, **limits)
    if fund.name == "Bills":
        return _result("bills", balance=balance, **limits)

    first_of_month = date(as_of.year, as_of.month, 1)
    window_end = first_of_month - timedelta(days=1)
    window_first = add_months(first_of_month, -window_months)

    created = _aware_utc(fund.created_at).astimezone(TIMEZONE).date()
    earliest = _earliest_txn_date(db, fund.id)
    history_start = min(created, earliest) if earliest is not None else created
    start = max(window_first, _first_full_month(history_start))
    months_used = _count_months(start, window_end)
    window_start = start if months_used else None
    frame = {
        "balance": balance,
        "months_used": months_used,
        "window_start": window_start,
        "window_end": window_end,
        **limits,
    }
    if months_used < min_months:
        return _result("too_new", **frame)

    total_in, total_out = _window_totals(db, fund.id, start, window_end)
    avg_in = money(total_in / months_used)
    avg_out = money(total_out / months_used)
    net_burn = money(avg_out - avg_in)
    frame.update(
        total_in=total_in,
        total_out=total_out,
        avg_in=avg_in,
        avg_out=avg_out,
        net_burn=net_burn,
    )

    if balance < 0:
        return _debt_result(as_of, balance, net_burn, cap_months, frame)
    if total_in == 0 and total_out == 0:
        return _result("no_activity", **frame)
    if net_burn < 0:
        return _result("growing", **frame)
    if net_burn == 0:
        return _result("flat", **frame)
    if balance == 0:
        return _result("empty", is_warning=True, **frame)
    return _running_down_result(as_of, balance, net_burn, warn_below_months, frame)


def _debt_result(
    as_of: date,
    balance: Decimal,
    net_burn: Decimal,
    cap_months: int,
    frame: dict,
) -> FundRunwayResult:
    net_inflow = money(-net_burn)
    if net_inflow <= 0:
        return _result("debt_stuck", is_warning=True, **frame)
    exact = abs(balance) / net_inflow
    capped = exact > cap_months
    return _result(
        "payoff",
        months_to_zero=exact.quantize(_TENTH, rounding=ROUND_UP),
        back_to_zero_date=None if capped else _shift_months(as_of, exact),
        is_capped=capped,
        is_warning=False,
        **frame,
    )


def _running_down_result(
    as_of: date,
    balance: Decimal,
    net_burn: Decimal,
    warn_below_months: int,
    frame: dict,
) -> FundRunwayResult:
    exact = balance / net_burn
    cap_months = int(frame["cap_months"])
    runway_months = exact.quantize(_TENTH, rounding=ROUND_DOWN)
    if exact > cap_months:
        return _result(
            "running_down",
            runway_months=runway_months,
            is_capped=True,
            is_warning=False,
            **frame,
        )
    return _result(
        "running_down",
        runway_months=runway_months,
        run_out_date=_shift_months(as_of, runway_months),
        is_capped=False,
        is_warning=runway_months < warn_below_months,
        **frame,
    )
