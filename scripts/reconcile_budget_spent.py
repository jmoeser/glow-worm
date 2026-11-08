"""Repair stored budget spent, and the overflow sweeps that used it.

Before budget expenses updated Budget.spent_amount from every path, stored
spent missed those rows. On the 1st of the next month the overflow sweep
used that stored figure, so the overflow fund can be high or low for a
past month.

The correct spent for a budget is the sum of transaction amounts that
``_counts_as_budget_spend`` counts: an expense whose transaction type is
``budget_expense`` and whose ``budget_id`` is set. Stored spent is never
set below zero (``_write_spent``).

Default is a dry run: print the gaps and the overflow correction for each
month that already has a surplus sweep or shortfall, and write nothing.
``--apply`` writes both in one database transaction. A second run finds
nothing to change.

A contribution uses the same type pair as a surplus sweep (``transfer`` /
``contribution``). A withdrawal uses ``expense`` / ``withdrawal``. The
correction amount is the month's correct spent minus its stored spent, so
it matches the sweep's error when that stored figure is still the one the
sweep used. A month with no sweep is left out of the overflow fund. A
deleted or missing overflow fund is left unchanged; spent is still corrected.

Local:

    uv run python scripts/reconcile_budget_spent.py
    uv run python scripts/reconcile_budget_spent.py --apply

Docker (the image copies ``scripts/`` and puts the venv on ``PATH``):

    docker exec -it <container-id> python scripts/reconcile_budget_spent.py
    docker exec -it <container-id> python scripts/reconcile_budget_spent.py --apply
"""

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.database import SessionLocal
from app.models import Budget, Category, SinkingFund, Transaction
from app.services.sweep_descriptions import (
    SHORTFALL_PREFIX as _SHORTFALL_PREFIX,
)
from app.services.sweep_descriptions import (
    SURPLUS_PREFIX as _SURPLUS_PREFIX,
)
from app.services.sweep_descriptions import (  # noqa: F401
    correction_description,
    month_label,
    parse_sweep,
    sweep_descriptions,
)
from app.services.transaction_balances import _counts_as_budget_spend, _write_spent

_CENT = Decimal("0.01")
_POST_ACTIONS = ("withdrawal", "contribution")


def _today() -> date:
    """Return today's date in the configured timezone."""
    return datetime.now(TIMEZONE).date()


def _money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(_CENT)


def _correct_spent(raw: Decimal) -> Decimal:
    """Figure ``_write_spent`` would store for a ledger sum of ``raw``."""
    probe = Budget(
        category_id=1,
        month=1,
        year=2000,
        allocated_amount=0,
        spent_amount=0,
        fund_balance=0,
    )
    _write_spent(probe, raw)
    return _money(probe.spent_amount)


@dataclass
class BudgetDiff:
    budget_id: int
    category: str
    month: int
    year: int
    stored: Decimal
    correct: Decimal
    raw: Decimal

    @property
    def difference(self) -> Decimal:
        return (self.correct - self.stored).quantize(_CENT)


@dataclass
class OverflowFix:
    month: int
    year: int
    action: str
    amount: Decimal
    fund_id: int | None
    fund_name: str | None
    category_id: int | None
    skip_reason: str | None


@dataclass
class Report:
    checked: int
    diffs: list[BudgetDiff]
    overflow: list[OverflowFix]
    clamp_notes: list[str]


def _ledger_totals(db: Session) -> dict[int, Decimal]:
    totals: dict[int, Decimal] = {}
    rows = db.query(Transaction).filter(Transaction.budget_id.is_not(None)).all()
    for txn in rows:
        budget_id = txn.budget_id
        if budget_id is None:
            continue
        if not _counts_as_budget_spend(txn.type, txn.transaction_type, budget_id):
            continue
        totals[budget_id] = totals.get(budget_id, Decimal(0)) + Decimal(str(txn.amount))
    return totals


def _swept_months(db: Session) -> dict[tuple[int, int], list[Transaction]]:
    rows = (
        db.query(Transaction)
        .filter(
            or_(
                Transaction.description.like(f"{_SURPLUS_PREFIX}%"),
                Transaction.description.like(f"{_SHORTFALL_PREFIX}%"),
            )
        )
        .all()
    )
    grouped: dict[tuple[int, int], list[Transaction]] = {}
    for txn in rows:
        parsed = parse_sweep(txn.description)
        if parsed is None:
            continue
        month, year = parsed
        grouped.setdefault((year, month), []).append(txn)
    return grouped


def _overflow_fix(
    db: Session,
    year: int,
    month: int,
    txns: list[Transaction],
    net: Decimal,
) -> OverflowFix:
    blank = OverflowFix(
        month=month,
        year=year,
        action="none",
        amount=Decimal("0.00"),
        fund_id=None,
        fund_name=None,
        category_id=None,
        skip_reason=None,
    )
    if net == 0:
        return blank

    fund_ids = [txn.sinking_fund_id for txn in txns]
    if any(fund_id is None for fund_id in fund_ids) or len(set(fund_ids)) != 1:
        blank.action = "skipped"
        blank.skip_reason = "sweep transactions do not share one sinking fund"
        return blank

    fund = db.get(SinkingFund, fund_ids[0])
    if fund is None:
        blank.action = "skipped"
        blank.skip_reason = "overflow fund is missing"
        return blank
    blank.fund_id = fund.id
    blank.fund_name = fund.name
    if fund.is_deleted:
        blank.action = "skipped"
        blank.skip_reason = f"overflow fund {fund.name} is deleted"
        return blank

    category_ids = {txn.category_id for txn in txns}
    if len(category_ids) != 1:
        blank.action = "skipped"
        blank.skip_reason = "sweep transactions do not share one category"
        return blank

    blank.category_id = category_ids.pop()
    if net > 0:
        blank.action = "withdrawal"
        blank.amount = net
    else:
        blank.action = "contribution"
        blank.amount = -net
    return blank


def build_report(db: Session) -> Report:
    names = {row.id: row.name for row in db.query(Category).all()}
    totals = _ledger_totals(db)
    diffs: list[BudgetDiff] = []
    clamp_notes: list[str] = []
    net_by_month: dict[tuple[int, int], Decimal] = {}

    budgets = db.query(Budget).all()
    for budget in budgets:
        raw = totals.get(budget.id, Decimal(0))
        correct = _correct_spent(raw)
        stored = _money(budget.spent_amount)
        key = (budget.year, budget.month)
        net_by_month[key] = net_by_month.get(key, Decimal(0)) + (correct - stored)
        category = names.get(budget.category_id, f"category {budget.category_id}")
        if correct == 0 and raw < 0:
            clamp_notes.append(
                f"Clamp: {category} {month_label(budget.month, budget.year)} "
                f"ledger sum is {raw.quantize(_CENT)}, which is below zero, "
                "so spent is stored as 0.00."
            )
        if stored == correct:
            continue
        diffs.append(
            BudgetDiff(
                budget_id=budget.id,
                category=category,
                month=budget.month,
                year=budget.year,
                stored=stored,
                correct=correct,
                raw=raw,
            )
        )

    diffs.sort(key=lambda row: (row.year, row.month, row.category, row.budget_id))
    clamp_notes.sort()

    overflow: list[OverflowFix] = []
    for (year, month), txns in sorted(_swept_months(db).items()):
        net = net_by_month.get((year, month), Decimal(0)).quantize(_CENT)
        overflow.append(_overflow_fix(db, year, month, txns, net))

    return Report(
        checked=len(budgets),
        diffs=diffs,
        overflow=overflow,
        clamp_notes=clamp_notes,
    )


def _column_line(cells: tuple[str, ...], widths: list[int]) -> str:
    padded: list[str] = []
    for index, cell in enumerate(cells):
        if index < 2:
            padded.append(cell.ljust(widths[index]))
        else:
            padded.append(cell.rjust(widths[index]))
    return "  ".join(padded)


def _print_report(report: Report) -> None:
    headers = ("Category", "Month", "Stored", "Correct", "Difference")
    body = [
        (
            row.category,
            month_label(row.month, row.year),
            f"{row.stored:.2f}",
            f"{row.correct:.2f}",
            f"{row.difference:+.2f}",
        )
        for row in report.diffs
    ]
    widths = [len(header) for header in headers]
    for record in body:
        for index, cell in enumerate(record):
            widths[index] = max(widths[index], len(cell))

    print("Budget spent")
    print(_column_line(headers, widths))
    if not body:
        print("(no differences)")
    for record in body:
        print(_column_line(record, widths))

    print()
    print("Overflow corrections")
    if not report.overflow:
        print("(no swept months)")
    for fix in report.overflow:
        label = month_label(fix.month, fix.year)
        if fix.action in _POST_ACTIONS:
            fund = fix.fund_name or ""
            print(f"{label}  {fix.action}  {fix.amount:.2f}  {fund}")
        elif fix.action == "skipped":
            print(f"{label}  skipped  {fix.skip_reason}")
        else:
            print(f"{label}  no correction")

    print()
    for note in report.clamp_notes:
        print(note)
    if report.clamp_notes:
        print()

    posted = sum(1 for fix in report.overflow if fix.action in _POST_ACTIONS)
    print(
        f"Budgets checked: {report.checked}. "
        f"Differing: {len(report.diffs)}. "
        f"Overflow corrections: {posted}."
    )


def _apply(db: Session, report: Report) -> None:
    for diff in report.diffs:
        budget = db.get(Budget, diff.budget_id)
        if budget is None:
            raise RuntimeError(f"Budget {diff.budget_id} disappeared; rolled back.")
        _write_spent(budget, diff.raw)

    for fix in report.overflow:
        if fix.action not in _POST_ACTIONS:
            continue
        if fix.fund_id is None or fix.category_id is None:
            raise RuntimeError("Overflow correction is missing a fund or category.")
        fund = db.get(SinkingFund, fix.fund_id)
        label = month_label(fix.month, fix.year)
        if fund is None or fund.is_deleted:
            raise RuntimeError(
                f"Overflow fund for {label} is missing or deleted; rolled back."
            )
        if fix.action == "contribution":
            fund.current_balance = float(
                Decimal(str(fund.current_balance)) + fix.amount
            )
            txn_type = "transfer"
            transaction_type = "contribution"
        else:
            fund.current_balance = float(
                Decimal(str(fund.current_balance)) - fix.amount
            )
            txn_type = "expense"
            transaction_type = "withdrawal"
        db.add(
            Transaction(
                date=_today().isoformat(),
                description=correction_description(fix.month, fix.year),
                amount=float(fix.amount),
                category_id=fix.category_id,
                type=txn_type,
                transaction_type=transaction_type,
                sinking_fund_id=fund.id,
            )
        )


def _has_writes(report: Report) -> bool:
    if report.diffs:
        return True
    return any(fix.action in _POST_ACTIONS for fix in report.overflow)


def reconcile(*, apply: bool = False, db: Session | None = None) -> Report:
    """Print the repair plan. With ``apply``, write it in one transaction."""
    managed = db is None
    if managed:
        db = SessionLocal()
    assert db is not None
    try:
        report = build_report(db)
        _print_report(report)
        if not apply:
            print("Dry run: no changes written.")
            return report
        if not _has_writes(report):
            print("Nothing to apply.")
            return report
        _apply(db, report)
        db.commit()
        print("Applied.")
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        if managed:
            db.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Repair Budget.spent_amount and past overflow sweeps."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write spent amounts and overflow corrections. Default is a dry run.",
    )
    args = parser.parse_args(argv)
    reconcile(apply=args.apply)


if __name__ == "__main__":
    main()
