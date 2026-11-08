"""Inspect this month's income distribution. Dry-run unless ``--apply``.

Prints whether the current month is already processed, whether legacy
evidence was bootstrapped, and whether automatic processing is paused for
the ambiguous upgrade month. ``--apply`` posts that month once. It can
double-post if the original system rows were edited until neither the
system description nor a sweep dated in the month remains.

No ``--month`` flag. Old months are not replayed with today's settings.

Local:

    uv run python scripts/review_monthly_processing.py
    uv run python scripts/review_monthly_processing.py --apply

Docker:

    docker exec -it <container-id> python scripts/review_monthly_processing.py
    docker exec -it <container-id> python scripts/review_monthly_processing.py --apply
"""

import argparse
from decimal import Decimal

from app.database import SessionLocal
from app.models import HouseholdMonth, HouseholdProcessing, MonthlyUnallocatedIncome
from app.services.clock import today
from app.services.household_months import ensure_backfill, is_held, is_processed
from app.tasks import _load_allocation_parts, process_income_allocation


def _primary_unallocated(db) -> Decimal | None:
    loaded = _load_allocation_parts(db)
    if loaded is None:
        return None
    allocation, _income_cat, _transfer_cat = loaded
    from app.tasks import _compute_bills_recommended

    income_amount = Decimal(str(allocation.monthly_income_amount))
    total = Decimal(str(allocation.monthly_budget_allocation))
    from app.models import SinkingFund

    bills = (
        db.query(SinkingFund)
        .filter(SinkingFund.name == "Bills", SinkingFund.is_deleted == False)
        .first()
    )
    bills_id = bills.id if bills else None
    for junction in allocation.sinking_fund_allocations:
        if junction.sinking_fund_id == bills_id:
            continue
        amount = Decimal(str(junction.allocation_amount))
        if amount > 0:
            fund = db.get(SinkingFund, junction.sinking_fund_id)
            if fund is not None and not fund.is_deleted:
                total += amount
    if bills is not None:
        if allocation.bills_fund_allocation_type == "fixed":
            bills_amount = Decimal(str(allocation.bills_fund_fixed_amount or 0))
        else:
            bills_amount = _compute_bills_recommended(db)
        if bills_amount > 0:
            total += bills_amount
    for transfer in allocation.recurring_transfers:
        total += Decimal(str(transfer.amount))
    return (income_amount - total).quantize(Decimal("0.01"))


def report(db) -> str:
    """Return the dry-run text. Does not post transactions."""
    current = today()
    ensure_backfill(db, today_value=current)
    year, month = current.year, current.month
    held = is_held(db, year, month)
    processed = is_processed(db, year, month)
    meta = db.get(HouseholdProcessing, 1)
    row = (
        db.query(HouseholdMonth)
        .filter(HouseholdMonth.year == year, HouseholdMonth.month == month)
        .first()
    )
    lines = [
        f"Month: {current.isoformat()} ({year}-{month:02d})",
        f"Processed: {'yes' if processed else 'no'}",
        f"Upgrade hold: {'yes' if held else 'no'}",
        f"Bootstrap: {meta.backfilled_at if meta is not None else 'missing'}",
    ]
    if row is not None:
        lines.append(f"Settlement: {row.settlement_outcome or 'unset'}")
        lines.append(f"Settlement fund id: {row.settlement_fund_id}")
    else:
        lines.append("Settlement: no month row")
    existing = (
        db.query(MonthlyUnallocatedIncome)
        .filter(
            MonthlyUnallocatedIncome.month == month,
            MonthlyUnallocatedIncome.year == year,
        )
        .first()
    )
    primary = _primary_unallocated(db)
    if existing is not None and primary is not None:
        current_amount = Decimal(str(existing.unallocated_amount))
        lines.append(
            "Unallocated: "
            f"stored {current_amount:.2f}, "
            f"--apply would store {(current_amount + primary):.2f}"
        )
    elif primary is not None:
        lines.append(f"Unallocated: --apply would store {primary:.2f}")
    if held:
        lines.append(
            "WARNING: automatic processing is paused for this ambiguous "
            "upgrade month until it is reviewed."
        )
        lines.append(
            "WARNING: --apply will post this month's distribution. "
            "That can double-post if the original system rows were edited "
            "until neither the system income description nor a sweep dated "
            "in the month remains."
        )
    elif processed:
        lines.append("--apply will not post again.")
    else:
        lines.append(
            "--apply will post this month's distribution if it is not already processed."
        )
    return "\n".join(lines)


def apply_current_month(db) -> int:
    """Post the current month once. Returns 0 when it is processed afterwards."""
    current = today()
    print(report(db))
    if is_processed(db, current.year, current.month):
        print("Already processed. Nothing posted.")
        return 0
    process_income_allocation(db=db, force=True)
    if is_processed(db, current.year, current.month):
        print("Posted this month's distribution.")
        return 0
    print("Nothing posted. Income allocation or a required category is missing.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Review or post the current month.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Post the current month. Default is a dry run.",
    )
    args = parser.parse_args()
    db = SessionLocal()
    try:
        if args.apply:
            return apply_current_month(db)
        print(report(db))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
