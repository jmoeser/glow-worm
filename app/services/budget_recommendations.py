"""Advisory budget allocation recommendations from recent spend history."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from sqlalchemy.orm import Session, joinedload

from app.models import Budget
from app.schemas import BudgetRecommendation, BudgetRecommendationSummary

_DEFAULT_LOOKBACK = 6
_DEFAULT_MIN_MONTHS = 2
_DEFAULT_MIN_DELTA_ABS = Decimal("20")
_DEFAULT_MIN_DELTA_PCT = Decimal("0.10")


def _prior_months(month: int, year: int, n: int) -> list[tuple[int, int]]:
    """Return n (month, year) pairs immediately before the given month."""
    result: list[tuple[int, int]] = []
    m, y = month, year
    for _ in range(n):
        if m == 1:
            m, y = 12, y - 1
        else:
            m -= 1
        result.append((m, y))
    return result


def _to_decimal(value: object) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def compute_recommendations(
    db: Session,
    month: int,
    year: int,
    *,
    lookback: int = _DEFAULT_LOOKBACK,
    min_months: int = _DEFAULT_MIN_MONTHS,
    min_delta_abs: Decimal = _DEFAULT_MIN_DELTA_ABS,
    min_delta_pct: Decimal = _DEFAULT_MIN_DELTA_PCT,
) -> list[BudgetRecommendation]:
    """Compare current-month allocations to average spent over recent months.

    Uses completed months only (excludes the given month). Missing history months
    are omitted from the sample rather than treated as zero spend.
    """
    current_budgets = (
        db.query(Budget)
        .options(joinedload(Budget.category))
        .filter(Budget.month == month, Budget.year == year)
        .all()
    )
    if not current_budgets:
        return []

    history_keys = _prior_months(month, year, lookback)
    if not history_keys:
        return []

    category_ids = [b.category_id for b in current_budgets]
    # Build OR-friendly month/year filter: pull all budgets for these categories
    # in the lookback window.
    min_year = min(y for _, y in history_keys)
    max_year = max(y for _, y in history_keys)
    history_rows = (
        db.query(Budget)
        .filter(
            Budget.category_id.in_(category_ids),
            Budget.year >= min_year,
            Budget.year <= max_year,
        )
        .all()
    )
    history_set = set(history_keys)
    # category_id -> list of spends in lookback order (most recent first)
    spends_by_cat: dict[int, list[Decimal]] = {cid: [] for cid in category_ids}
    history_map: dict[tuple[int, int, int], Decimal] = {}
    for row in history_rows:
        key = (row.category_id, row.month, row.year)
        if (row.month, row.year) in history_set:
            history_map[key] = _to_decimal(row.spent_amount)

    for cat_id in category_ids:
        for m, y in history_keys:
            spent = history_map.get((cat_id, m, y))
            if spent is not None:
                spends_by_cat[cat_id].append(spent)

    recommendations: list[BudgetRecommendation] = []
    for budget in current_budgets:
        samples = spends_by_cat.get(budget.category_id, [])
        if len(samples) < min_months:
            continue

        total = sum(samples, Decimal("0"))
        recommended = (total / len(samples)).quantize(Decimal("0.01"))
        current_allocated = _to_decimal(budget.allocated_amount)
        delta = (recommended - current_allocated).quantize(Decimal("0.01"))

        threshold = max(
            min_delta_abs,
            (abs(current_allocated) * min_delta_pct).quantize(Decimal("0.01")),
        )
        if abs(delta) < threshold:
            continue

        direction: Literal["raise", "lower"] = "raise" if delta > 0 else "lower"
        category = budget.category
        recommendations.append(
            BudgetRecommendation(
                budget_id=budget.id,
                category_id=budget.category_id,
                category_name=category.name
                if category
                else f"Category {budget.category_id}",
                category_color=category.color if category else "#888888",
                current_allocated=current_allocated,
                recommended=recommended,
                delta=delta,
                direction=direction,
                months_used=len(samples),
                sample_spends=list(samples),
            )
        )

    recommendations.sort(key=lambda r: abs(r.delta), reverse=True)
    return recommendations


def recommendation_summary(
    db: Session,
    month: int,
    year: int,
    *,
    lookback: int = _DEFAULT_LOOKBACK,
    min_months: int = _DEFAULT_MIN_MONTHS,
    min_delta_abs: Decimal = _DEFAULT_MIN_DELTA_ABS,
    min_delta_pct: Decimal = _DEFAULT_MIN_DELTA_PCT,
) -> BudgetRecommendationSummary:
    """Aggregate recommendations for dashboard teaser / net impact display."""
    items = compute_recommendations(
        db,
        month,
        year,
        lookback=lookback,
        min_months=min_months,
        min_delta_abs=min_delta_abs,
        min_delta_pct=min_delta_pct,
    )
    net_delta = sum((r.delta for r in items), Decimal("0")).quantize(Decimal("0.01"))
    return BudgetRecommendationSummary(
        count=len(items),
        net_delta=net_delta,
        items=items,
    )
