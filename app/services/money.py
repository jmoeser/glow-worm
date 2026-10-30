"""Shared money and calendar helpers."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

_CENT = Decimal("0.01")


def money(value: object) -> Decimal:
    """Quantize a money amount to cents."""
    return Decimal(str(value or 0)).quantize(_CENT)


def dollars(value: Decimal) -> str:
    """Whole dollars when the amount has no cents, otherwise the usual cents format."""
    amount = money(value)
    sign = "-" if amount < 0 else ""
    amount = abs(amount)
    if amount == amount.to_integral_value():
        body = f"{int(amount):,}"
    else:
        body = f"{amount:,.2f}"
    return f"{sign}${body}"


def add_months(current: date, months: int) -> date:
    """Add calendar months, clamping the day the way relativedelta does."""
    month_index = current.month - 1 + months
    year = current.year + month_index // 12
    month = month_index % 12 + 1
    day = min(current.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
