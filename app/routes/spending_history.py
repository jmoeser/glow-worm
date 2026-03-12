import calendar
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.database import get_db
from app.middleware import get_current_user
from app.models import Category, Transaction
from app.templating import templates

router = APIRouter()


def _current_year() -> int:
    return datetime.now(TIMEZONE).year


def _build_spending_matrix(
    db: Session,
    year: int,
    categories: list[Category],
) -> tuple[
    dict[int, dict[int, Decimal]],
    dict[int, Decimal],
    dict[int, Decimal],
    Decimal,
]:
    """Return (matrix, row_totals, col_totals, grand_total).

    matrix[month][category_id] = total spent
    """
    rows = (
        db.query(Transaction.date, Transaction.category_id, Transaction.amount)
        .filter(
            Transaction.type == "expense",
            Transaction.transaction_type.in_(["regular", "budget_expense"]),
            Transaction.date >= f"{year:04d}-01-01",
            Transaction.date <= f"{year:04d}-12-31",
            Transaction.category_id.isnot(None),
        )
        .all()
    )

    matrix: dict[int, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for date, cat_id, amount in rows:
        month_num = int(date[5:7])  # YYYY-MM-DD
        matrix[month_num][cat_id] += Decimal(str(amount or 0))

    cat_ids = {c.id for c in categories}
    row_totals: dict[int, Decimal] = {}
    col_totals: dict[int, Decimal] = defaultdict(Decimal)
    grand_total = Decimal("0")

    for month_num in range(1, 13):
        row_total = sum(
            (v for k, v in matrix[month_num].items() if k in cat_ids),
            Decimal("0"),
        )
        row_totals[month_num] = row_total.quantize(Decimal("0.01"))
        grand_total += row_total
        for cat in categories:
            col_totals[cat.id] += matrix[month_num].get(cat.id, Decimal("0"))

    for cat_id in col_totals:
        col_totals[cat_id] = col_totals[cat_id].quantize(Decimal("0.01"))

    return matrix, row_totals, dict(col_totals), grand_total.quantize(Decimal("0.01"))


@router.get("/spending-history", response_class=HTMLResponse)
async def spending_history_page(
    request: Request,
    year: int | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request)
    if year is None:
        year = _current_year()

    categories = (
        db.query(Category)
        .filter(
            Category.is_deleted == False,  # noqa: E712
            Category.type == "expense",
        )
        .order_by(Category.name)
        .all()
    )

    matrix, row_totals, col_totals, grand_total = _build_spending_matrix(
        db, year, categories
    )

    return templates.TemplateResponse(
        request,
        "spending_history.html",
        {
            "username": user.username,
            "year": year,
            "prev_year": year - 1,
            "next_year": year + 1,
            "categories": categories,
            "matrix": matrix,
            "row_totals": row_totals,
            "col_totals": col_totals,
            "grand_total": grand_total,
            "month_names": [calendar.month_abbr[m] for m in range(1, 13)],
        },
    )
