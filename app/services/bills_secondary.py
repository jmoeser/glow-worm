"""Bills-fund secondary income goal, and what is already recorded.

Shared by the dashboard cover check and the Bills forecast. Lives here so
``app.tasks`` and ``app.services.bills_cover`` do not import each other.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import SecondaryIncomeAllocation, Transaction
from app.services.money import money


def bills_secondary_goal(db: Session, fund_id: int) -> Decimal:
    """Sum of secondary-income goals aimed at this sinking fund."""
    allocation = db.query(SecondaryIncomeAllocation).first()
    if allocation is None:
        return Decimal("0.00")
    total = Decimal("0.00")
    for rule in allocation.rules:
        if rule.sinking_fund_id == fund_id:
            total += money(rule.goal_amount)
    return money(total)


def secondary_recorded(db: Session, fund_id: int, year: int, month: int) -> Decimal:
    """Secondary already booked to this fund in the calendar month.

    Matches the secondary-income page: ``secondary_income_allocation`` rows
    whose date falls in that month, including future-dated rows. Surplus
    overflow uses ``income_allocation`` and is not goal progress.
    """
    prefix = f"{year:04d}-{month:02d}"
    rows = (
        db.query(Transaction)
        .filter(
            Transaction.transaction_type == "secondary_income_allocation",
            Transaction.sinking_fund_id == fund_id,
            Transaction.date.like(f"{prefix}%"),
        )
        .all()
    )
    return money(sum((money(row.amount) for row in rows), Decimal(0)))
