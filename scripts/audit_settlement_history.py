"""Read-only audit of historical budget settlement.

Compares budget rows stored now with system sweep transactions, month
records, repair corrections, late clawbacks, and a short list of possible
manual transfers. It does not post, repair, bootstrap, or update anything.
A missing automatic sweep is not reported as missing money.

Local:

    uv run python scripts/audit_settlement_history.py
    uv run python scripts/audit_settlement_history.py --from 2026-01 --to 2026-06

Docker (the image copies ``scripts/`` and puts the venv on ``PATH``):

    docker exec -it <container-id> python scripts/audit_settlement_history.py
    docker exec -it <container-id> python scripts/audit_settlement_history.py --from 2026-01 --to 2026-06

There is no ``--apply``.
"""

import argparse
import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.database import SessionLocal
from app.models import (
    Budget,
    HouseholdMonth,
    IncomeAllocation,
    SinkingFund,
    Transaction,
)
from app.services.clock import today
from app.services.sweep_descriptions import (
    month_label,
    parse_correction,
    parse_income_description,
    parse_sweep_kind,
)

_CENT = Decimal("0.01")
_MAX_LINES = 8
_TAKEN_BACK = "Taken back for "
_CLOSE_LEAD_DAYS = 3
_CLOSE_FOLLOW_DAYS = 14


@dataclass(frozen=True)
class _TxnView:
    txn_id: int
    txn_date: str
    amount: Decimal
    fund_id: int | None
    fund_name: str | None
    fund_deleted: bool
    description: str
    kind: str


def _money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(_CENT)


def _fmt(value: Decimal) -> str:
    return f"{value.quantize(_CENT):.2f}"


def _next_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _iter_months(start: tuple[int, int], end: tuple[int, int]):
    year, month = start
    while (year, month) <= end:
        yield year, month
        year, month = _next_month(year, month)


def _parse_year_month(text: str) -> tuple[int, int]:
    pieces = text.split("-")
    if len(pieces) != 2 or len(pieces[0]) != 4:
        raise argparse.ArgumentTypeError("use YYYY-MM")
    try:
        year = int(pieces[0])
        month = int(pieces[1])
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use YYYY-MM") from exc
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError("month must be 01-12")
    return year, month


def _close_window(year: int, month: int) -> tuple[str, str]:
    last = calendar.monthrange(year, month)[1]
    start_day = max(1, last - (_CLOSE_LEAD_DAYS - 1))
    next_year, next_month = _next_month(year, month)
    return (
        f"{year}-{month:02d}-{start_day:02d}",
        f"{next_year}-{next_month:02d}-{_CLOSE_FOLLOW_DAYS:02d}",
    )


def _in_window(iso_date: str, year: int, month: int) -> bool:
    start, end = _close_window(year, month)
    return start <= iso_date <= end


def _fund_label(fund_id: int | None, funds: dict[int, SinkingFund]) -> str:
    if fund_id is None:
        return "no fund"
    fund = funds.get(fund_id)
    if fund is None:
        return f"fund {fund_id} (row missing)"
    deleted = " deleted" if fund.is_deleted else ""
    return f'fund {fund_id} "{fund.name}"{deleted}'


def _view(txn: Transaction, funds: dict[int, SinkingFund], kind: str) -> _TxnView:
    fund = funds.get(txn.sinking_fund_id) if txn.sinking_fund_id is not None else None
    return _TxnView(
        txn_id=txn.id,
        txn_date=txn.date,
        amount=_money(txn.amount),
        fund_id=txn.sinking_fund_id,
        fund_name=None if fund is None else fund.name,
        fund_deleted=bool(fund is not None and fund.is_deleted),
        description=txn.description or "",
        kind=kind,
    )


def _is_clawback(description: str | None, month: int, year: int) -> bool:
    if not description or not description.startswith(_TAKEN_BACK):
        return False
    return f"({month_label(month, year)} " in description


def _manual_kind(txn: Transaction) -> str | None:
    if txn.sinking_fund_id is None:
        return None
    if txn.type == "transfer" and txn.transaction_type == "contribution":
        return "contribution"
    if txn.type == "expense" and txn.transaction_type == "withdrawal":
        return "withdrawal"
    return None


def _load_month_rows(db: Session) -> dict[tuple[int, int], HouseholdMonth] | None:
    if not inspect(db.get_bind()).has_table(HouseholdMonth.__tablename__):
        return None
    return {(row.year, row.month): row for row in db.query(HouseholdMonth).all()}


def _evidence_months(
    budgets: list[Budget],
    txns: list[Transaction],
    month_rows: dict[tuple[int, int], HouseholdMonth] | None,
) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    for budget in budgets:
        found.add((budget.year, budget.month))
    for txn in txns:
        sweep = parse_sweep_kind(txn.description)
        if sweep is not None:
            _kind, month, year = sweep
            found.add((year, month))
        correction = parse_correction(txn.description)
        if correction is not None:
            month, year = correction
            found.add((year, month))
        income = parse_income_description(txn.description)
        if income is not None:
            month, year = income
            if txn.date.startswith(f"{year}-{month:02d}-"):
                found.add((year, month))
                found.add(_previous_month(year, month))
    if month_rows is not None:
        for key, row in month_rows.items():
            found.add(key)
            if row.processed_at is not None:
                found.add(_previous_month(key[0], key[1]))
    return found


def _truncate(lines: list[str], noun: str) -> list[str]:
    if len(lines) <= _MAX_LINES:
        return lines
    hidden = len(lines) - _MAX_LINES
    shown = lines[:_MAX_LINES]
    shown.append(
        f"    showing {_MAX_LINES} of {len(lines)}. {hidden} more {noun} not shown."
    )
    return shown


def _render_month(
    year: int,
    month: int,
    budgets: list[Budget],
    txns: list[Transaction],
    funds: dict[int, SinkingFund],
    month_row: HouseholdMonth | None,
    records_available: bool,
) -> list[str]:
    label = f"{year}-{month:02d} {month_label(month, year)}"
    if not budgets:
        reconstructed = None
        head = f"{label}  current rows: unknown (no budget rows; not treated as zero)"
    else:
        total = sum(
            (
                _money(row.allocated_amount)
                - _money(row.spent_amount)
                + _money(row.fund_balance)
                for row in budgets
            ),
            Decimal("0.00"),
        ).quantize(_CENT)
        count = len(budgets)
        noun = "budget row" if count == 1 else "budget rows"
        if total > 0:
            head = f"{label}  current rows: surplus {_fmt(total)} ({count} {noun})"
        elif total < 0:
            head = (
                f"{label}  current rows: shortfall {_fmt(abs(total))} ({count} {noun})"
            )
        else:
            head = f"{label}  current rows: zero ({count} {noun})"
        reconstructed = total

    automatic = []
    for txn in txns:
        parsed = parse_sweep_kind(txn.description)
        if parsed is None:
            continue
        kind, sweep_month, sweep_year = parsed
        if sweep_month == month and sweep_year == year:
            automatic.append(_view(txn, funds, kind))
    corrections = [
        _view(txn, funds, "correction")
        for txn in txns
        if parse_correction(txn.description) == (month, year)
    ]
    clawbacks = [
        _view(txn, funds, "clawback")
        for txn in txns
        if _is_clawback(txn.description, month, year)
    ]
    excluded = {row.txn_id for row in automatic}
    excluded.update(row.txn_id for row in corrections)
    excluded.update(row.txn_id for row in clawbacks)
    manual = [
        _view(txn, funds, kind)
        for txn in txns
        if txn.id not in excluded
        and (kind := _manual_kind(txn)) is not None
        and _in_window(txn.date, year, month)
        and parse_sweep_kind(txn.description) is None
        and parse_correction(txn.description) is None
        and not _is_clawback(txn.description, month, year)
    ]
    lines = [
        head,
        *_automatic_lines(automatic),
        _record_line(month_row, records_available, funds),
    ]
    lines.append("  Corrections (repair postings, not added to the automatic amount):")
    if corrections:
        lines.extend(
            _truncate(
                [_txn_line(row) for row in corrections],
                "corrections",
            )
        )
    else:
        lines.append("    none")
    lines.append("  Late clawbacks naming this month (not the month's settlement):")
    if clawbacks:
        lines.extend(_truncate([_txn_line(row) for row in clawbacks], "clawbacks"))
    else:
        lines.append("    none")
    lines.append(
        "  Manual suggestions (not proven; amount and date do not link a transfer):"
    )
    if manual:
        lines.extend(_truncate([_txn_line(row) for row in manual], "suggestions"))
        lines.append(
            "    Window: last 3 days of the month through day 14 of the next month. "
            "System sweeps, corrections, clawbacks, and income-allocation rows are excluded."
        )
    else:
        lines.append("    none in the close window")
    lines.extend(
        _notes(reconstructed, automatic, corrections, clawbacks, month_row, budgets)
    )
    return lines


def _txn_line(row: _TxnView) -> str:
    name = ""
    if row.fund_name is not None:
        name = f' "{row.fund_name}"'
    deleted = " deleted" if row.fund_deleted else ""
    fund = "no fund" if row.fund_id is None else f"fund {row.fund_id}{name}{deleted}"
    return (
        f"    txn {row.txn_id} {row.txn_date} {row.kind} {_fmt(row.amount)} {fund} "
        f"{row.description}"
    )


def _automatic_lines(rows: list[_TxnView]) -> list[str]:
    if not rows:
        return ["  Automatic posting: none"]
    kinds = {row.kind for row in rows}
    funds = {row.fund_id for row in rows}
    lines = ["  Automatic posting:"]
    lines.extend(_truncate([_txn_line(row) for row in rows], "automatic rows"))
    if len(rows) > 1 and len(kinds) == 1 and len(funds) == 1 and None not in funds:
        lines.append(
            "    More than one automatic posting. Amounts are not added together."
        )
    elif len(kinds) != 1 or len(funds) != 1 or None in funds:
        lines.append(
            "    These automatic postings disagree. No single settlement is identified."
        )
    return lines


def _record_line(
    row: HouseholdMonth | None,
    available: bool,
    funds: dict[int, SinkingFund],
) -> str:
    if not available:
        return "  Month record: not in this database (migration not applied)"
    if row is None:
        return "  Month record: none"
    distributed = (
        "distributed" if row.processed_at is not None else "not recorded as distributed"
    )
    outcome = row.settlement_outcome or "settlement unset"
    amount = (
        "amount unset"
        if row.settlement_amount is None
        else f"amount {_fmt(_money(row.settlement_amount))}"
    )
    fund = _fund_label(row.settlement_fund_id, funds)
    txn = (
        "no settlement transaction"
        if row.settlement_transaction_id is None
        else (f"settlement txn {row.settlement_transaction_id}")
    )
    settled = row.settled_on or "no settled_on"
    return f"  Month record: {outcome}, {distributed}, {amount}, {fund}, {txn}, settled_on {settled}"


def _notes(
    reconstructed: Decimal | None,
    automatic: list[_TxnView],
    corrections: list[_TxnView],
    clawbacks: list[_TxnView],
    month_row: HouseholdMonth | None,
    budgets: list[Budget],
) -> list[str]:
    notes = [
        (
            "  Current rows are not the amount due on the original close. "
            "Late confirms and later budget edits change them."
        ),
    ]
    if not budgets:
        notes.append("  Absent budget rows are unknown, not a zero settlement.")
    elif reconstructed == 0 and not automatic:
        notes.append(
            "  Current rows net to zero. A zero close posts no transaction. "
            "This does not show whether an overflow fund was configured then."
        )
    elif reconstructed is not None and not automatic:
        notes.append(
            "  No automatic sweep or shortfall was found. "
            "That does not mean the month was left unsettled. "
            "It may have been zero at the time, had no overflow fund, or been moved by hand."
        )
    elif len(automatic) == 1 and reconstructed is not None:
        posted = automatic[0]
        signed = posted.amount if posted.kind == "surplus" else -posted.amount
        delta = (reconstructed - signed).quantize(_CENT)
        if delta == 0:
            notes.append(
                "  Current rows match this automatic posting's amount. "
                "That is not a reconstruction of the settings on the day it ran."
            )
        else:
            notes.append(
                f"  Current rows differ from this automatic posting by {_fmt(delta)}. "
                "That is not proof of missing money."
            )
    elif len(automatic) > 1:
        notes.append(
            "  Several automatic postings name this month. "
            "They are listed separately and are not proof of one settlement amount."
        )
    if corrections:
        notes.append(
            "  A correction explains a later repair, not the original close. "
            "It is not added to the automatic posting."
        )
    if clawbacks:
        notes.append(
            "  A late clawback can move spent and a fund after the close. "
            "It is not the month's settlement."
        )
    if month_row is not None and month_row.settlement_outcome in {
        "zero",
        "no_overflow",
    }:
        notes.append(
            f"  The stored outcome is {month_row.settlement_outcome}. "
            "That is a recorded fact, not a recalculation from today's rows."
        )
    if month_row is not None and month_row.settlement_outcome == "unresolved":
        notes.append(
            "  The month record is unresolved because sweep evidence disagreed. "
            "No fund was chosen."
        )
    return notes


def render_audit(
    db: Session,
    *,
    today_value: date,
    start: tuple[int, int] | None = None,
    end: tuple[int, int] | None = None,
) -> str:
    """Printable audit. Reads the session and does not commit or insert."""
    last_completed = _previous_month(today_value.year, today_value.month)
    completed = last_completed
    clipped = False
    if end is not None and end < completed:
        completed = end
    elif end is not None and end > last_completed:
        clipped = True
    budgets = db.query(Budget).all()
    txns = db.query(Transaction).order_by(Transaction.id).all()
    funds = {fund.id: fund for fund in db.query(SinkingFund).all()}
    month_rows = _load_month_rows(db)
    evidence = _evidence_months(budgets, txns, month_rows)
    evidence = {key for key in evidence if key <= completed}
    if start is not None:
        evidence = {key for key in evidence if key >= start}
        range_start = start if start <= completed else None
    elif evidence:
        range_start = min(evidence)
    else:
        range_start = None
    lines = [
        "Historical settlement audit (read only). Nothing is written.",
        f"Today: {today_value.isoformat()} ({TIMEZONE.zone}).",
        "Completed months stop the month before today. The current month is not closed here.",
        "Caveats:",
        "- Figures use budget rows stored now: allocated - spent + fund balance.",
        "- Historical income settings and the overflow fund of that month were not stored.",
        "- Today's overflow setting is not used as the historical fund.",
        "- A missing automatic posting does not mean the settlement was missed.",
        "- Manual lines are suggestions. Matching an amount or a date is not a link.",
        "- Corrections and late clawbacks are shown apart from the automatic posting.",
        "- Months with no budget rows are unknown, not zero.",
        "- Outside the span of evidence, months with nothing to inspect are omitted, not reported as zero.",
        "- An explicit --from/--to lists every completed month in that range.",
    ]
    allocation = None
    if inspect(db.get_bind()).has_table(IncomeAllocation.__tablename__):
        allocation = db.query(IncomeAllocation).first()
    if allocation is None or allocation.overflow_sinking_fund_id is None:
        lines.append(
            "Today's overflow fund: none configured. "
            "Historical months are not assumed to have had none."
        )
    else:
        lines.append(
            f"Today's overflow fund id is {allocation.overflow_sinking_fund_id}. "
            "It is not treated as the fund for any past month."
        )
    if month_rows is None:
        lines.append("Month records: household_months is not in this database.")
    else:
        lines.append(
            "Month records: read if present. This audit does not create or fill them."
        )
    if clipped:
        lines.append(
            "The requested end is after the last completed month. Later months are omitted."
        )
    if range_start is None or range_start > completed:
        lines.append(
            "No completed month in this range has budget rows or processing evidence."
        )
        return "\n".join(lines)
    lines.append(
        f"Range: {range_start[0]}-{range_start[1]:02d} through {completed[0]}-{completed[1]:02d}."
    )
    by_budget: dict[tuple[int, int], list[Budget]] = {}
    for budget in budgets:
        by_budget.setdefault((budget.year, budget.month), []).append(budget)
    for key in _iter_months(range_start, completed):
        lines.append("")
        record = None if month_rows is None else month_rows.get(key)
        lines.extend(
            _render_month(
                key[0],
                key[1],
                by_budget.get(key, []),
                txns,
                funds,
                record,
                month_rows is not None,
            )
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only audit of past budget settlements. Writes nothing."
    )
    parser.add_argument("--from", dest="start", type=_parse_year_month, default=None)
    parser.add_argument("--to", dest="end", type=_parse_year_month, default=None)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.start is not None and args.end is not None and args.start > args.end:
        print("--from is after --to")
        return 2
    db = SessionLocal()
    try:
        db.autoflush = False
        print(
            render_audit(
                db,
                today_value=today(),
                start=args.start,
                end=args.end,
            )
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
