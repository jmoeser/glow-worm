"""Read-only historical settlement audit. The script must not write."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Budget,
    Category,
    HouseholdMonth,
    HouseholdProcessing,
    SinkingFund,
    Transaction,
)
from app.services.sweep_descriptions import (
    correction_description,
    income_description,
    parse_correction,
    parse_sweep,
    sweep_descriptions,
)
from scripts.audit_settlement_history import build_parser, render_audit

TODAY = date(2026, 11, 8)
ROOT = Path(__file__).resolve().parents[1]


def _forbid_writes(monkeypatch, db):
    def _boom(*_args, **_kwargs):
        raise AssertionError("audit tried to write")

    monkeypatch.setattr(db, "commit", _boom)
    monkeypatch.setattr(db, "flush", _boom)
    monkeypatch.setattr(db, "add", _boom)
    monkeypatch.setattr(db, "delete", _boom)


def _category(db) -> Category:
    cat = Category(name="Groceries", type="expense", color="#22C55E")
    db.add(cat)
    db.commit()
    return cat


def _fund(db, name: str, balance: str = "0") -> SinkingFund:
    fund = SinkingFund(name=name, color="#112233", current_balance=Decimal(balance))
    db.add(fund)
    db.commit()
    return fund


def _budget(db, cat, month, year, allocated, spent, fund_balance="0") -> Budget:
    row = Budget(
        category_id=cat.id,
        month=month,
        year=year,
        allocated_amount=Decimal(allocated),
        spent_amount=Decimal(spent),
        fund_balance=Decimal(fund_balance),
    )
    db.add(row)
    db.commit()
    return row


def _txn(db, cat, **kwargs) -> Transaction:
    values = {
        "date": "2026-02-01",
        "amount": Decimal(10),
        "category_id": cat.id,
        "type": "transfer",
        "transaction_type": "contribution",
        "description": "note",
    }
    values.update(kwargs)
    txn = Transaction(**values)
    db.add(txn)
    db.commit()
    db.refresh(txn)
    return txn


def _section(text: str, year: int, month: int) -> str:
    marker = f"{year}-{month:02d} "
    parts = text.split("\n\n")
    for part in parts:
        if part.startswith(marker):
            return part
    raise AssertionError(f"{marker} not in audit:\n{text}")


def _snapshot(db):
    return (
        db.query(Transaction).count(),
        [
            (
                row.id,
                str(row.allocated_amount),
                str(row.spent_amount),
                str(row.fund_balance),
            )
            for row in db.query(Budget).order_by(Budget.id)
        ],
        [
            (row.id, str(row.current_balance))
            for row in db.query(SinkingFund).order_by(SinkingFund.id)
        ],
        db.query(HouseholdMonth).count(),
        db.query(HouseholdProcessing).count(),
    )


def test_parser_rejects_apply():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--apply"])


def test_source_does_not_bootstrap_or_repair():
    source = (ROOT / "scripts" / "audit_settlement_history.py").read_text()
    assert "ensure_backfill" not in source
    assert "process_income_allocation" not in source
    assert 'add_argument("--apply"' not in source


def test_correction_description_is_not_a_sweep():
    text = correction_description(1, 2026)
    assert parse_correction(text) == (1, 2026)
    assert parse_sweep(text) is None
    assert parse_correction(sweep_descriptions(1, 2026)[0]) is None


def test_audit_does_not_write_and_is_stable(db_session, monkeypatch):
    cat = _category(db_session)
    fund = _fund(db_session, "Overflow", "40")
    _budget(db_session, cat, 1, 2026, "500", "300")
    surplus, _short = sweep_descriptions(1, 2026)
    _txn(
        db_session,
        cat,
        date="2026-02-01",
        description=surplus,
        amount=Decimal(200),
        sinking_fund_id=fund.id,
    )
    before = _snapshot(db_session)
    _forbid_writes(monkeypatch, db_session)
    first = render_audit(db_session, today_value=TODAY)
    second = render_audit(db_session, today_value=TODAY)
    assert first == second
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted
    monkeypatch.undo()
    assert _snapshot(db_session) == before
    section = _section(first, 2026, 1)
    assert "surplus 200.00" in section
    assert "Automatic posting:" in section
    assert "not a reconstruction" in section
    assert "missing money" not in first


def test_shortfall_and_manual_suggestion_are_labeled(db_session, monkeypatch):
    cat = _category(db_session)
    fund = _fund(db_session, "Rainy", "10")
    _budget(db_session, cat, 6, 2026, "100", "180")
    manual = _txn(
        db_session,
        cat,
        date="2026-07-01",
        description="June leftover moved by hand",
        amount=Decimal(80),
        type="expense",
        transaction_type="withdrawal",
        sinking_fund_id=fund.id,
    )
    late = _txn(
        db_session,
        cat,
        date="2026-07-20",
        description="Too late to be the close",
        amount=Decimal(80),
        type="expense",
        transaction_type="withdrawal",
        sinking_fund_id=fund.id,
    )
    allocation = _txn(
        db_session,
        cat,
        date="2026-07-01",
        description="Income allocation to Rainy",
        amount=Decimal(80),
        type="transfer",
        transaction_type="income_allocation",
        sinking_fund_id=fund.id,
    )
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 6), end=(2026, 6))
    section = _section(text, 2026, 6)
    assert "shortfall 80.00" in section
    assert f"txn {manual.id}" in section
    assert "suggestion" in section.lower() or "Suggestions" in section
    assert "does not mean the month was left unsettled" in section
    assert f"txn {late.id}" not in section
    assert f"txn {allocation.id}" not in section
    assert "not proof" in text or "does not mean" in section
    assert "missing money" not in text


def test_zero_rows_are_not_called_a_settlement(db_session, monkeypatch):
    cat = _category(db_session)
    _budget(db_session, cat, 2, 2026, "100", "100")
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 2), end=(2026, 2))
    section = _section(text, 2026, 2)
    assert "current rows: zero" in section
    assert "posts no transaction" in section
    assert "does not show whether an overflow fund was configured" in section
    assert "missing money" not in text


def test_absent_budgets_with_a_later_run_are_unknown(db_session, monkeypatch):
    cat = _category(db_session)
    _txn(
        db_session,
        cat,
        date="2026-04-01",
        description=income_description(4, 2026),
        amount=Decimal(1),
        type="income",
        transaction_type="income",
    )
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 3), end=(2026, 4))
    march = _section(text, 2026, 3)
    april = _section(text, 2026, 4)
    assert "unknown" in march
    assert "not treated as zero" in march
    assert "surplus 0.00" not in march
    assert "shortfall 0.00" not in march
    assert "unknown" in april
    assert "2026-05" not in text


def test_no_overflow_record_is_not_recalculated(db_session, monkeypatch):
    cat = _category(db_session)
    _budget(db_session, cat, 5, 2026, "40", "10")
    db_session.add(
        HouseholdMonth(
            year=2026,
            month=5,
            settlement_outcome="no_overflow",
            settlement_amount=0,
            settled_on="2026-06-01",
        )
    )
    db_session.commit()
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 5), end=(2026, 5))
    section = _section(text, 2026, 5)
    assert "Month record: no_overflow" in section
    assert "recorded fact" in section
    assert "surplus 30.00" in section
    assert "missing money" not in text


def test_legacy_sweep_without_a_month_row(db_session, monkeypatch):
    cat = _category(db_session)
    fund = _fund(db_session, "Old pot", "5")
    surplus, _short = sweep_descriptions(1, 2026)
    posted = _txn(
        db_session,
        cat,
        date="2026-02-01",
        description=surplus,
        amount=Decimal(15),
        sinking_fund_id=fund.id,
    )
    assert db_session.query(HouseholdMonth).count() == 0
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 1), end=(2026, 1))
    section = _section(text, 2026, 1)
    assert f"txn {posted.id}" in section
    assert "Month record: none" in section
    assert "does not create or fill" in text
    monkeypatch.undo()
    assert db_session.query(HouseholdMonth).count() == 0


def test_recorded_settlement_can_differ_from_current_rows(db_session, monkeypatch):
    cat = _category(db_session)
    fund = _fund(db_session, "Overflow", "9")
    _budget(db_session, cat, 1, 2026, "500", "450")
    surplus, _short = sweep_descriptions(1, 2026)
    posted = _txn(
        db_session,
        cat,
        date="2026-02-01",
        description=surplus,
        amount=Decimal(200),
        sinking_fund_id=fund.id,
    )
    db_session.add(
        HouseholdMonth(
            year=2026,
            month=1,
            processed_at=None,
            settlement_outcome="surplus",
            settlement_fund_id=fund.id,
            settlement_transaction_id=posted.id,
            settlement_amount=Decimal(200),
            settled_on="2026-02-01",
        )
    )
    db_session.commit()
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 1), end=(2026, 1))
    section = _section(text, 2026, 1)
    assert "surplus 50.00" in section
    assert "Month record: surplus" in section
    assert f"settlement txn {posted.id}" in section
    assert f'fund {fund.id} "{fund.name}"' in section
    assert "differ from this automatic posting" in section
    assert "not proof of missing money" in section


def test_duplicate_and_conflicting_sweeps(db_session, monkeypatch):
    cat = _category(db_session)
    first = _fund(db_session, "One", "1")
    second = _fund(db_session, "Two", "2")
    _budget(db_session, cat, 3, 2026, "10", "0")
    _budget(db_session, cat, 4, 2026, "10", "0")
    march_surplus, _rest = sweep_descriptions(3, 2026)
    april_surplus, _rest = sweep_descriptions(4, 2026)
    one = _txn(
        db_session,
        cat,
        date="2026-04-01",
        description=march_surplus,
        amount=Decimal(10),
        sinking_fund_id=first.id,
    )
    two = _txn(
        db_session,
        cat,
        date="2026-04-02",
        description=march_surplus,
        amount=Decimal(10),
        sinking_fund_id=first.id,
    )
    left = _txn(
        db_session,
        cat,
        date="2026-05-01",
        description=april_surplus,
        amount=Decimal(10),
        sinking_fund_id=first.id,
    )
    right = _txn(
        db_session,
        cat,
        date="2026-05-01",
        description=april_surplus,
        amount=Decimal(4),
        sinking_fund_id=second.id,
    )
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 3), end=(2026, 4))
    march = _section(text, 2026, 3)
    april = _section(text, 2026, 4)
    assert f"txn {one.id}" in march
    assert f"txn {two.id}" in march
    assert "not added together" in march
    assert "20.00" not in march.split("Automatic posting:")[1].split("Month record:")[0]
    assert f"txn {left.id}" in april
    assert f"txn {right.id}" in april
    assert "disagree" in april
    assert "missing money" not in text


def test_correction_and_late_clawback_are_separate(db_session, monkeypatch):
    cat = _category(db_session)
    fund = _fund(db_session, "Overflow", "20")
    _budget(db_session, cat, 1, 2026, "500", "450")
    surplus, _short = sweep_descriptions(1, 2026)
    _txn(
        db_session,
        cat,
        date="2026-02-01",
        description=surplus,
        amount=Decimal(200),
        sinking_fund_id=fund.id,
    )
    correction = _txn(
        db_session,
        cat,
        date="2026-03-01",
        description=correction_description(1, 2026),
        amount=Decimal(150),
        type="expense",
        transaction_type="withdrawal",
        sinking_fund_id=fund.id,
    )
    claw = _txn(
        db_session,
        cat,
        date="2026-02-20",
        description="Taken back for Rent (January 2026 Groceries)",
        amount=Decimal(150),
        type="expense",
        transaction_type="withdrawal",
        sinking_fund_id=fund.id,
    )
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 1), end=(2026, 1))
    section = _section(text, 2026, 1)
    assert f"txn {correction.id}" in section
    assert "not added to the automatic posting" in section
    assert f"txn {claw.id}" in section
    assert "not the month's settlement" in section
    assert "Late confirms and later budget edits" in section
    assert "not proof of missing money" in section
    manual = section.split("Manual suggestions")[1]
    assert f"txn {correction.id}" not in manual
    assert f"txn {claw.id}" not in manual


def test_manual_list_truncates(db_session, monkeypatch):
    cat = _category(db_session)
    fund = _fund(db_session, "Rainy", "1")
    _budget(db_session, cat, 8, 2026, "5", "0")
    for index in range(9):
        _txn(
            db_session,
            cat,
            date="2026-09-01",
            description=f"Hand move {index}",
            amount=Decimal(1),
            sinking_fund_id=fund.id,
        )
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY, start=(2026, 8), end=(2026, 8))
    section = _section(text, 2026, 8)
    assert "showing 8 of 9" in section
    assert "1 more suggestions not shown" in section


def test_current_month_is_omitted(db_session, monkeypatch):
    cat = _category(db_session)
    _budget(db_session, cat, 11, 2026, "20", "0")
    _budget(db_session, cat, 10, 2026, "20", "5")
    _forbid_writes(monkeypatch, db_session)
    text = render_audit(db_session, today_value=TODAY)
    assert "\n2026-11 " not in f"\n{text}"
    assert "2026-10 " in text


def test_audit_works_before_the_month_table_exists(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    for table in (
        Category.__table__,
        SinkingFund.__table__,
        Budget.__table__,
        Transaction.__table__,
    ):
        table.create(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    cat = Category(name="Groceries", type="expense", color="#22C55E")
    db.add(cat)
    db.flush()
    fund = SinkingFund(name="Pot", color="#abcdef", current_balance=3)
    db.add(fund)
    db.flush()
    surplus, _short = sweep_descriptions(1, 2026)
    db.add(
        Budget(
            category_id=cat.id,
            month=1,
            year=2026,
            allocated_amount=8,
            spent_amount=3,
            fund_balance=0,
        )
    )
    db.add(
        Transaction(
            date="2026-02-01",
            description=surplus,
            amount=5,
            category_id=cat.id,
            type="transfer",
            transaction_type="contribution",
            sinking_fund_id=fund.id,
        )
    )
    db.commit()
    _forbid_writes(monkeypatch, db)
    text = render_audit(db, today_value=TODAY, start=(2026, 1), end=(2026, 1))
    assert "household_months is not in this database" in text
    assert "surplus 5.00" in text
    assert "Automatic posting:" in text
    db.close()
    engine.dispose()
