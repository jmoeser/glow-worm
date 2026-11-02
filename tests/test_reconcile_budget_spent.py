"""One-off repair of Budget.spent_amount and the sweeps that used it."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models import Budget, Category, SinkingFund, Transaction
from scripts.reconcile_budget_spent import (
    correction_description,
    reconcile,
    sweep_descriptions,
)

_TODAY = date(2026, 11, 2)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(
        "scripts.reconcile_budget_spent._today",
        lambda: _TODAY,
    )


def _money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _category(db: Session, name: str, cat_type: str = "expense") -> Category:
    row = Category(name=name, type=cat_type, color="#334455", is_budget_category=True)
    db.add(row)
    db.flush()
    return row


def _fund(
    db: Session, name: str, balance: str, *, deleted: bool = False
) -> SinkingFund:
    row = SinkingFund(
        name=name,
        color="#AABBCC",
        current_balance=Decimal(balance),
        is_deleted=deleted,
    )
    db.add(row)
    db.flush()
    return row


def _budget(
    db: Session,
    category: Category,
    month: int,
    year: int,
    spent: str,
) -> Budget:
    row = Budget(
        category_id=category.id,
        month=month,
        year=year,
        allocated_amount=Decimal("500.00"),
        spent_amount=Decimal(spent),
        fund_balance=Decimal("0.00"),
    )
    db.add(row)
    db.flush()
    return row


def _txn(
    db: Session,
    category: Category,
    budget: Budget | None,
    amount: str,
    *,
    txn_type: str = "expense",
    transaction_type: str = "budget_expense",
    fund: SinkingFund | None = None,
    description: str = "Spend",
    when: str = "2026-03-15",
) -> Transaction:
    row = Transaction(
        date=when,
        description=description,
        amount=Decimal(amount),
        category_id=category.id,
        type=txn_type,
        transaction_type=transaction_type,
        budget_id=budget.id if budget is not None else None,
        sinking_fund_id=fund.id if fund is not None else None,
    )
    db.add(row)
    db.flush()
    return row


def _sweep(
    db: Session,
    category: Category,
    fund: SinkingFund,
    month: int,
    year: int,
    *,
    shortfall: bool = False,
) -> Transaction:
    surplus, short = sweep_descriptions(month, year)
    return _txn(
        db,
        category,
        None,
        "200.00",
        txn_type="expense" if shortfall else "transfer",
        transaction_type="withdrawal" if shortfall else "contribution",
        fund=fund,
        description=short if shortfall else surplus,
        when=f"{year}-{month:02d}-01",
    )


def _corrections(db: Session) -> list[Transaction]:
    prefix = "Budget correction \u2014 "
    return (
        db.query(Transaction)
        .filter(Transaction.description.like(f"{prefix}%"))
        .order_by(Transaction.id)
        .all()
    )


def _old_bug_case(db: Session) -> dict:
    """Budgets whose stored spent ignores ledger rows, plus real sweeps.

    March: groceries ledger 40 against stored 0, dining ledger 5 against
    stored 20. Net spent is 25 higher than stored, so the sweep paid the
    overflow fund 25 too much.
    April: transport ledger 20 against stored 30, so the fund was short 10.
    May already matches and has a sweep, so it needs no correction.
    A regular expense and a budget transfer sit on groceries and must not count.
    """
    transfer = _category(db, "Transfer", "transfer")
    groceries = _category(db, "Groceries")
    dining = _category(db, "Dining")
    transport = _category(db, "Transport")
    fund = _fund(db, "Short Term Savings", "5.00")

    march_g = _budget(db, groceries, 3, 2026, "0.00")
    march_d = _budget(db, dining, 3, 2026, "20.00")
    april_t = _budget(db, transport, 4, 2026, "30.00")
    may_g = _budget(db, groceries, 5, 2026, "12.00")

    _txn(db, groceries, march_g, "40.00")
    _txn(
        db,
        groceries,
        march_g,
        "999.00",
        transaction_type="regular",
        description="Not a budget expense",
    )
    _txn(
        db,
        groceries,
        march_g,
        "100.00",
        transaction_type="budget_transfer",
        txn_type="transfer",
        description="Move into the budget",
    )
    _txn(db, dining, march_d, "5.00", when="2026-03-20")
    _txn(db, transport, april_t, "20.00", when="2026-04-02")
    _txn(db, groceries, may_g, "12.00", when="2026-05-02")

    _sweep(db, transfer, fund, 3, 2026)
    _sweep(db, transfer, fund, 4, 2026, shortfall=True)
    _sweep(db, transfer, fund, 5, 2026)
    db.commit()
    return {"fund": fund, "march_g": march_g, "march_d": march_d, "april_t": april_t}


class TestReconcileBudgetSpent:
    def test_dry_run_prints_the_gap_and_writes_nothing(self, db_session, capsys):
        case = _old_bug_case(db_session)
        fund_id = case["fund"].id

        report = reconcile(db=db_session)
        out = capsys.readouterr().out

        assert [(row.category, row.difference) for row in report.diffs] == [
            ("Dining", Decimal("-15.00")),
            ("Groceries", Decimal("40.00")),
            ("Transport", Decimal("-10.00")),
        ]
        by_month = {fix.month: fix for fix in report.overflow}
        assert by_month[3].action == "withdrawal"
        assert by_month[3].amount == Decimal("25.00")
        assert by_month[3].fund_name == "Short Term Savings"
        assert by_month[4].action == "contribution"
        assert by_month[4].amount == Decimal("10.00")
        assert by_month[5].action == "none"
        assert "Groceries" in out
        assert "March 2026" in out
        assert "+40.00" in out
        assert "March 2026  withdrawal  25.00  Short Term Savings" in out
        assert "April 2026  contribution  10.00  Short Term Savings" in out
        assert "May 2026  no correction" in out
        assert "Differing: 3." in out
        assert "Dry run: no changes written." in out

        db_session.expire_all()
        assert _money(
            db_session.get(Budget, case["march_g"].id).spent_amount
        ) == Decimal("0.00")
        assert _money(db_session.get(SinkingFund, fund_id).current_balance) == Decimal(
            "5.00"
        )
        assert _corrections(db_session) == []

    def test_apply_posts_both_overflow_directions(self, db_session, capsys):
        case = _old_bug_case(db_session)
        fund_id = case["fund"].id

        reconcile(db=db_session, apply=True)
        out = capsys.readouterr().out
        db_session.expire_all()

        assert _money(
            db_session.get(Budget, case["march_g"].id).spent_amount
        ) == Decimal("40.00")
        assert _money(
            db_session.get(Budget, case["march_d"].id).spent_amount
        ) == Decimal("5.00")
        assert _money(
            db_session.get(Budget, case["april_t"].id).spent_amount
        ) == Decimal("20.00")
        # 5.00 - 25.00 withdrawal + 10.00 contribution. The fund may go negative.
        assert _money(db_session.get(SinkingFund, fund_id).current_balance) == Decimal(
            "-10.00"
        )

        corrections = {txn.description: txn for txn in _corrections(db_session)}
        assert set(corrections) == {
            correction_description(3, 2026),
            correction_description(4, 2026),
        }
        withdrawal = corrections[correction_description(3, 2026)]
        contribution = corrections[correction_description(4, 2026)]
        assert withdrawal.type == "expense"
        assert withdrawal.transaction_type == "withdrawal"
        assert _money(withdrawal.amount) == Decimal("25.00")
        assert withdrawal.date == "2026-11-02"
        assert withdrawal.sinking_fund_id == fund_id
        assert contribution.type == "transfer"
        assert contribution.transaction_type == "contribution"
        assert _money(contribution.amount) == Decimal("10.00")
        assert contribution.sinking_fund_id == fund_id
        assert "Applied." in out

    def test_second_apply_posts_nothing(self, db_session, capsys):
        case = _old_bug_case(db_session)
        fund_id = case["fund"].id
        reconcile(db=db_session, apply=True)
        capsys.readouterr()
        db_session.expire_all()
        spent = _money(db_session.get(Budget, case["march_g"].id).spent_amount)
        balance = _money(db_session.get(SinkingFund, fund_id).current_balance)
        correction_ids = [txn.id for txn in _corrections(db_session)]

        report = reconcile(db=db_session, apply=True)
        out = capsys.readouterr().out
        db_session.expire_all()

        assert report.diffs == []
        assert all(fix.action == "none" for fix in report.overflow)
        assert "Differing: 0." in out
        assert "Overflow corrections: 0." in out
        assert "Nothing to apply." in out
        assert "(no differences)" in out
        assert _money(db_session.get(Budget, case["march_g"].id).spent_amount) == spent
        assert _money(db_session.get(SinkingFund, fund_id).current_balance) == balance
        assert [txn.id for txn in _corrections(db_session)] == correction_ids

    def test_month_without_a_sweep_does_not_move_overflow(self, db_session, capsys):
        transfer = _category(db_session, "Transfer", "transfer")
        groceries = _category(db_session, "Groceries")
        fund = _fund(db_session, "Short Term Savings", "50.00")
        budget = _budget(db_session, groceries, 11, 2026, "0.00")
        _txn(db_session, groceries, budget, "15.50", when="2026-11-01")
        # The transfer category is unused: this month was never swept.
        assert transfer.id
        db_session.commit()

        report = reconcile(db=db_session, apply=True)
        db_session.expire_all()

        assert report.overflow == []
        assert report.diffs[0].difference == Decimal("15.50")
        assert _money(db_session.get(Budget, budget.id).spent_amount) == Decimal(
            "15.50"
        )
        assert _money(db_session.get(SinkingFund, fund.id).current_balance) == Decimal(
            "50.00"
        )
        assert _corrections(db_session) == []
        assert "(no swept months)" in capsys.readouterr().out

    def test_deleted_overflow_fund_skips_the_correction(self, db_session, capsys):
        transfer = _category(db_session, "Transfer", "transfer")
        groceries = _category(db_session, "Groceries")
        fund = _fund(db_session, "Old Overflow", "80.00", deleted=True)
        budget = _budget(db_session, groceries, 1, 2026, "0.00")
        _txn(db_session, groceries, budget, "12.00", when="2026-01-08")
        _sweep(db_session, transfer, fund, 1, 2026)
        db_session.commit()

        report = reconcile(db=db_session, apply=True)
        out = capsys.readouterr().out
        db_session.expire_all()

        assert report.overflow[0].action == "skipped"
        assert report.overflow[0].skip_reason == "overflow fund Old Overflow is deleted"
        assert "overflow fund Old Overflow is deleted" in out
        assert _money(db_session.get(Budget, budget.id).spent_amount) == Decimal(
            "12.00"
        )
        assert _money(db_session.get(SinkingFund, fund.id).current_balance) == Decimal(
            "80.00"
        )
        assert _corrections(db_session) == []

        capsys.readouterr()
        again = reconcile(db=db_session, apply=True)
        db_session.expire_all()
        assert again.diffs == []
        assert again.overflow[0].action == "none"
        assert _corrections(db_session) == []
        assert _money(db_session.get(SinkingFund, fund.id).current_balance) == Decimal(
            "80.00"
        )

    def test_negative_ledger_sum_is_clamped_and_stays_put(self, db_session, capsys):
        transfer = _category(db_session, "Transfer", "transfer")
        groceries = _category(db_session, "Groceries")
        fund = _fund(db_session, "Short Term Savings", "40.00")
        budget = _budget(db_session, groceries, 2, 2026, "5.00")
        _txn(db_session, groceries, budget, "-8.00", when="2026-02-02")
        _sweep(db_session, transfer, fund, 2, 2026)
        db_session.commit()

        report = reconcile(db=db_session, apply=True)
        out = capsys.readouterr().out
        db_session.expire_all()

        assert report.diffs[0].correct == Decimal("0.00")
        assert report.diffs[0].difference == Decimal("-5.00")
        assert report.overflow[0].action == "contribution"
        assert report.overflow[0].amount == Decimal("5.00")
        assert any("below zero" in note for note in report.clamp_notes)
        assert "below zero" in out
        assert _money(db_session.get(Budget, budget.id).spent_amount) == Decimal("0.00")
        assert _money(db_session.get(SinkingFund, fund.id).current_balance) == Decimal(
            "45.00"
        )

        capsys.readouterr()
        again = reconcile(db=db_session, apply=True)
        db_session.expire_all()
        assert again.diffs == []
        assert again.overflow[0].action == "none"
        assert len(_corrections(db_session)) == 1
        assert any("below zero" in note for note in again.clamp_notes)
        assert _money(db_session.get(SinkingFund, fund.id).current_balance) == Decimal(
            "45.00"
        )
