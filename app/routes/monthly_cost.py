from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.database import get_db
from app.middleware import get_current_user
from app.models import Category, Transaction
from app.templating import templates

router = APIRouter()

_INCLUDED_TYPES = ("regular", "budget_expense", "withdrawal")


def _build_monthly_cost_data(
    db: Session, _today: datetime | None = None, net: bool = False
) -> dict:
    today = _today or datetime.now(TIMEZONE)

    excluded_cat_ids = (
        db.query(Category.id)
        .filter(Category.exclude_from_monthly_cost == True)
        .scalar_subquery()
    )

    first_date_str: str | None = (
        db.query(func.min(Transaction.date))
        .filter(
            Transaction.type == "expense",
            Transaction.transaction_type.in_(_INCLUDED_TYPES),
            Transaction.category_id.not_in(excluded_cat_ids),
        )
        .scalar()
    )

    if not first_date_str:
        return {
            "rows": [],
            "grand_total": Decimal("0.00"),
            "grand_monthly_avg": Decimal("0.00"),
            "months_elapsed": 0,
            "first_date": None,
            "first_date_display": None,
            "net": net,
        }

    first_date = date.fromisoformat(first_date_str)
    months_elapsed = (
        (today.year - first_date.year) * 12 + (today.month - first_date.month) + 1
    )

    rows_raw = (
        db.query(Category, func.sum(Transaction.amount).label("total_spent"))
        .join(Transaction, Transaction.category_id == Category.id)
        .filter(
            Transaction.type == "expense",
            Transaction.transaction_type.in_(_INCLUDED_TYPES),
            Category.exclude_from_monthly_cost == False,
        )
        .group_by(Category.id)
        .order_by(func.sum(Transaction.amount).desc())
        .all()
    )

    offset_map: dict[int, Decimal] = {}
    if net:
        offset_rows = (
            db.query(
                Transaction.category_id,
                func.sum(Transaction.amount).label("total_offset"),
            )
            .join(Category, Category.id == Transaction.category_id)
            .filter(
                Transaction.transaction_type == "contribution",
                Category.exclude_from_monthly_cost == False,
            )
            .group_by(Transaction.category_id)
            .all()
        )
        offset_map = {
            r.category_id: Decimal(str(r.total_offset or 0)) for r in offset_rows
        }

    grand_total = Decimal(0)
    rows: list[dict] = []
    for row in rows_raw:
        total = Decimal(str(row.total_spent or 0))
        if net:
            total = total - offset_map.get(row.Category.id, Decimal(0))
        total = total.quantize(Decimal("0.01"))
        grand_total += total
        monthly_avg = (total / months_elapsed).quantize(Decimal("0.01"))
        rows.append(
            {
                "category": row.Category,
                "total_spent": total,
                "monthly_avg": monthly_avg,
            }
        )

    grand_monthly_avg = (grand_total / months_elapsed).quantize(Decimal("0.01"))

    for r in rows:
        r_total = r["total_spent"]
        r["pct_of_total"] = (
            (r_total / grand_total * 100).quantize(Decimal("0.1"))
            if grand_total
            else Decimal("0.0")
        )

    rows.sort(key=lambda r: r["total_spent"], reverse=True)

    return {
        "rows": rows,
        "grand_total": grand_total.quantize(Decimal("0.01")),
        "grand_monthly_avg": grand_monthly_avg,
        "months_elapsed": months_elapsed,
        "first_date": first_date_str,
        "first_date_display": first_date.strftime("%B %Y"),
        "net": net,
    }


@router.get("/monthly-cost", response_class=HTMLResponse)
async def monthly_cost_page(
    request: Request, net: bool = False, db: Session = Depends(get_db)
):
    user = get_current_user(request)
    data = _build_monthly_cost_data(db, net=net)
    return templates.TemplateResponse(
        request,
        "monthly_cost.html",
        {"username": user.username, **data},
    )


@router.get("/api/monthly-cost")
async def monthly_cost_api(request: Request, db: Session = Depends(get_db)):
    data = _build_monthly_cost_data(db)
    return JSONResponse(
        {
            "months_elapsed": data["months_elapsed"],
            "first_date": data["first_date"],
            "grand_total": float(data["grand_total"]),
            "grand_monthly_avg": float(data["grand_monthly_avg"]),
            "rows": [
                {
                    "category_id": r["category"].id,
                    "category_name": r["category"].name,
                    "category_color": r["category"].color,
                    "total_spent": float(r["total_spent"]),
                    "monthly_avg": float(r["monthly_avg"]),
                    "pct_of_total": float(r["pct_of_total"]),
                }
                for r in data["rows"]
            ],
        }
    )
