"""Month records for income distribution and overflow settlement.

The unique ``(year, month)`` upsert decides which monthly runner commits.
It does not lock sinking-fund balances. A concurrent bill, edit, or clawback
can still write a stale ``current_balance`` over this job, and a late confirm
can still miss a debit if it overlaps settlement. Those races are unchanged.
"""

import logging
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import HouseholdMonth, HouseholdProcessing, Transaction, _utcnow
from app.services.clock import as_configured, month_window_open, now, today
from app.services.sweep_descriptions import (
    INCOME_PREFIX,
    SHORTFALL_PREFIX,
    SURPLUS_PREFIX,
    parse_income_description,
    parse_sweep_kind,
)

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_CLAWBACK_OUTCOMES = frozenset({"surplus", "shortfall", "zero"})


def _insert(dialect_name: str):
    if dialect_name == "postgresql":
        return pg_insert
    if dialect_name == "sqlite":
        return sqlite_insert
    raise RuntimeError(f"unsupported database dialect {dialect_name}")


def _dialect_name(db: Session) -> str:
    return db.get_bind().dialect.name


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    import calendar

    last = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}-01", f"{year}-{month:02d}-{last:02d}"


def _date_in_month(iso_date: str, year: int, month: int) -> bool:
    start, end = _month_bounds(year, month)
    return start <= iso_date <= end


def _money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(_CENT)


def processed_claim_statement(
    dialect_name: str, year: int, month: int, processed_at: datetime
):
    """Conditional claim. A settlement-only row is updated; a processed row is not."""
    insert = _insert(dialect_name)
    stmt = insert(HouseholdMonth).values(
        year=year,
        month=month,
        processed_at=processed_at,
    )
    return stmt.on_conflict_do_update(
        index_elements=["year", "month"],
        set_={"processed_at": stmt.excluded.processed_at},
        where=HouseholdMonth.processed_at.is_(None),
    ).returning(HouseholdMonth.year, HouseholdMonth.month)


def settlement_claim_statement(
    dialect_name: str,
    year: int,
    month: int,
    outcome: str,
    fund_id: int | None,
    amount: Decimal | None,
    settled_on: str | None,
):
    insert = _insert(dialect_name)
    stmt = insert(HouseholdMonth).values(
        year=year,
        month=month,
        settlement_outcome=outcome,
        settlement_fund_id=fund_id,
        settlement_amount=amount,
        settled_on=settled_on,
    )
    return stmt.on_conflict_do_update(
        index_elements=["year", "month"],
        set_={
            "settlement_outcome": stmt.excluded.settlement_outcome,
            "settlement_fund_id": stmt.excluded.settlement_fund_id,
            "settlement_amount": stmt.excluded.settlement_amount,
            "settled_on": stmt.excluded.settled_on,
        },
        where=HouseholdMonth.settlement_outcome.is_(None),
    ).returning(HouseholdMonth.year, HouseholdMonth.month)


def claim_processed(db: Session, year: int, month: int) -> bool:
    stmt = processed_claim_statement(_dialect_name(db), year, month, _utcnow())
    row = db.execute(stmt).first()
    if row is None:
        return False
    claimed_year, claimed_month = row
    return claimed_year == year and claimed_month == month


def claim_settlement(
    db: Session,
    year: int,
    month: int,
    outcome: str,
    fund_id: int | None,
    amount: Decimal | None,
    settled_on: str,
) -> bool:
    stored_amount = None if amount is None else _money(amount)
    stmt = settlement_claim_statement(
        _dialect_name(db),
        year,
        month,
        outcome,
        fund_id,
        stored_amount,
        settled_on,
    )
    row = db.execute(stmt).first()
    return row is not None


def _meta(db: Session) -> HouseholdProcessing | None:
    return db.get(HouseholdProcessing, 1)


def _ensure_meta_row(db: Session) -> None:
    insert = _insert(_dialect_name(db))
    stmt = (
        insert(HouseholdProcessing)
        .values(id=1)
        .on_conflict_do_nothing(index_elements=["id"])
    )
    db.execute(stmt)
    db.flush()


def is_processed(db: Session, year: int, month: int) -> bool:
    found = (
        db.query(HouseholdMonth.processed_at)
        .filter(HouseholdMonth.year == year, HouseholdMonth.month == month)
        .first()
    )
    if found is None:
        return False
    processed_at = found[0]
    return processed_at is not None


def is_held(db: Session, year: int, month: int) -> bool:
    meta = _meta(db)
    return meta is not None and meta.held_year == year and meta.held_month == month


def has_processed_before(db: Session) -> bool:
    found = (
        db.query(HouseholdMonth.id)
        .filter(HouseholdMonth.processed_at.is_not(None))
        .first()
    )
    return found is not None


def record_attempt(db: Session, year: int, month: int) -> None:
    """Remember that this month was attempted, including a missing-config return."""
    meta = _meta(db)
    if meta is None:
        return
    if meta.attempt_year == year and meta.attempt_month == month:
        return
    meta.attempt_year = year
    meta.attempt_month = month
    db.commit()


def clear_hold(db: Session, year: int, month: int) -> None:
    meta = _meta(db)
    if meta is None:
        return
    if meta.held_year == year and meta.held_month == month:
        meta.held_year = None
        meta.held_month = None


def automatic_recovery_allowed(db: Session, moment: datetime | None = None) -> bool:
    """Startup and hourly retry. The 00:05 cron does not use this gate."""
    current = as_configured(now() if moment is None else moment)
    if not month_window_open(current):
        return False
    year, month = current.year, current.month
    if is_processed(db, year, month):
        return False
    if is_held(db, year, month):
        return False
    if has_processed_before(db):
        return True
    meta = _meta(db)
    if meta is not None and meta.attempt_year == year and meta.attempt_month == month:
        return True
    return current.day == 1


def _legacy_upsert_processed(
    db: Session,
    dialect_name: str,
    year: int,
    month: int,
    processed_at: datetime,
    income_id: int | None,
) -> None:
    insert = _insert(dialect_name)
    stmt = insert(HouseholdMonth).values(
        year=year,
        month=month,
        processed_at=processed_at,
        income_transaction_id=income_id,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["year", "month"],
        set_={
            "processed_at": stmt.excluded.processed_at,
            "income_transaction_id": stmt.excluded.income_transaction_id,
        },
        where=HouseholdMonth.processed_at.is_(None),
    )
    db.execute(stmt)


def _legacy_upsert_settlement(
    db: Session,
    dialect_name: str,
    year: int,
    month: int,
    outcome: str,
    fund_id: int | None,
    amount: Decimal | None,
    txn_id: int | None,
    settled_on: str | None,
) -> None:
    insert = _insert(dialect_name)
    stmt = insert(HouseholdMonth).values(
        year=year,
        month=month,
        settlement_outcome=outcome,
        settlement_fund_id=fund_id,
        settlement_amount=None if amount is None else _money(amount),
        settlement_transaction_id=txn_id,
        settled_on=settled_on,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["year", "month"],
        set_={
            "settlement_outcome": stmt.excluded.settlement_outcome,
            "settlement_fund_id": stmt.excluded.settlement_fund_id,
            "settlement_amount": stmt.excluded.settlement_amount,
            "settlement_transaction_id": stmt.excluded.settlement_transaction_id,
            "settled_on": stmt.excluded.settled_on,
        },
        where=HouseholdMonth.settlement_outcome.is_(None),
    )
    db.execute(stmt)


def _apply_legacy_evidence(
    db: Session, meta: HouseholdProcessing, current: date
) -> None:
    dialect_name = _dialect_name(db)
    rows = (
        db.query(Transaction)
        .filter(
            or_(
                Transaction.description.like(f"{INCOME_PREFIX}%"),
                Transaction.description.like(f"{SURPLUS_PREFIX}%"),
                Transaction.description.like(f"{SHORTFALL_PREFIX}%"),
            )
        )
        .order_by(Transaction.id)
        .all()
    )
    processed: dict[tuple[int, int], tuple[datetime, int | None]] = {}
    sweeps: dict[tuple[int, int], list[tuple[int, int, Decimal, str, str]]] = {}
    for txn in rows:
        income_month = parse_income_description(txn.description)
        if income_month is not None:
            named_month, named_year = income_month
            if _date_in_month(txn.date, named_year, named_month):
                key = (named_year, named_month)
                current_mark = processed.get(key)
                if current_mark is None or current_mark[1] is None:
                    processed[key] = (datetime.fromisoformat(txn.date), txn.id)
        sweep = parse_sweep_kind(txn.description)
        if sweep is None:
            continue
        kind, sweep_month, sweep_year = sweep
        posted = _year_month(txn.date)
        if posted is None:
            continue
        posted_year, posted_month = posted
        posted_key = (posted_year, posted_month)
        if posted_key not in processed:
            processed[posted_key] = (datetime.fromisoformat(txn.date), None)
        if txn.sinking_fund_id is None:
            continue
        sweeps.setdefault((sweep_year, sweep_month), []).append(
            (txn.id, txn.sinking_fund_id, _money(txn.amount), kind, txn.date)
        )

    for (year, month), (processed_at, income_id) in processed.items():
        _legacy_upsert_processed(db, dialect_name, year, month, processed_at, income_id)

    for (year, month), evidence in sweeps.items():
        fund_ids = {item[1] for item in evidence}
        kinds = {item[3] for item in evidence}
        if len(fund_ids) == 1 and len(kinds) == 1:
            chosen_id, fund_id, amount, kind, settled_on = min(
                evidence, key=lambda item: item[0]
            )
            _legacy_upsert_settlement(
                db,
                dialect_name,
                year,
                month,
                kind,
                fund_id,
                abs(amount),
                chosen_id,
                settled_on,
            )
            continue
        ids = sorted(item[0] for item in evidence)
        logger.warning(
            "household month %s-%02d has conflicting sweep rows; leaving it unresolved: %s",
            year,
            month,
            ids,
        )
        earliest = min(item[4] for item in evidence)
        _legacy_upsert_settlement(
            db,
            dialect_name,
            year,
            month,
            "unresolved",
            None,
            None,
            None,
            earliest,
        )

    current_key = (current.year, current.month)
    if current_key in processed:
        return
    start, end = _month_bounds(current.year, current.month)
    stray = (
        db.query(Transaction.id)
        .filter(
            Transaction.transaction_type == "income",
            Transaction.date >= start,
            Transaction.date <= end,
        )
        .first()
    )
    if stray is not None:
        meta.held_year = current.year
        meta.held_month = current.month
        logger.info(
            "Pausing automatic processing for %s-%02d until it is reviewed",
            current.year,
            current.month,
        )


def _year_month(iso_date: str) -> tuple[int, int] | None:
    if len(iso_date) < 7:
        return None
    try:
        year = int(iso_date[0:4])
        month = int(iso_date[5:7])
    except ValueError:
        return None
    if not 1 <= month <= 12:
        return None
    return year, month


def ensure_backfill(db: Session, *, today_value: date | None = None) -> None:
    """Commit legacy metadata once, before any writer moves money or claws back.

    A second call sees ``backfilled_at`` and does not read descriptions again.
    Two overlapping first scans only fill null columns and do not post money.
    The migration's meta row already exists, so this does not depend on an
    insert lock. PostgreSQL and SQLite both commit this before the money
    transaction that called it.
    """
    current = today() if today_value is None else today_value
    meta = _meta(db)
    if meta is not None and meta.backfilled_at is not None:
        return
    _ensure_meta_row(db)
    meta = _meta(db)
    if meta is None:
        raise RuntimeError("household processing row missing")
    if meta.backfilled_at is not None:
        return
    _apply_legacy_evidence(db, meta, current)
    meta.backfilled_at = _utcnow()
    db.commit()


def recover_current_month() -> None:
    """Startup catch-up and hourly retry. Does not bypass the upgrade hold."""
    from app.database import SessionLocal
    from app.tasks import process_income_allocation

    db = SessionLocal()
    try:
        moment = now()
        ensure_backfill(db, today_value=moment.date())
        if not automatic_recovery_allowed(db, moment):
            return
    finally:
        db.close()
    process_income_allocation()


def clawback_outcome(db: Session, year: int, month: int) -> tuple[str, int] | None:
    """Stored fund for a clawback, or None when this month must not debit overflow."""
    found = (
        db.query(HouseholdMonth.settlement_outcome, HouseholdMonth.settlement_fund_id)
        .filter(HouseholdMonth.year == year, HouseholdMonth.month == month)
        .first()
    )
    if found is None:
        return None
    outcome, fund_id = found
    if outcome not in _CLAWBACK_OUTCOMES or fund_id is None:
        return None
    return outcome, fund_id
