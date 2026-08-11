from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.database import get_db
from app.middleware import get_current_user
from app.models import (
    Category,
    IncomeAllocation,
    IncomeAllocationRecurringTransfer,
    IncomeAllocationToSinkingFund,
    MonthlyUnallocatedIncome,
    SecondaryIncomeAllocation,
    SecondaryIncomeAllocationRule,
    SinkingFund,
    Transaction,
)
from app.schemas import (
    IncomeAllocationCreate,
    IncomeAllocationRecurringTransferCreate,
    IncomeAllocationResponse,
    RecordSecondaryIncomeRequest,
    SecondaryIncomeAllocationCreate,
    SecondaryIncomeAllocationResponse,
)
from app.templating import templates

router = APIRouter()


def _upsert_allocation(
    db: Session,
    monthly_income_amount: Decimal,
    monthly_budget_allocation: Decimal,
    bills_fund_allocation_type: str,
    bills_fund_fixed_amount: Decimal | None,
    fund_allocations: list[dict],
    recurring_transfers: list[IncomeAllocationRecurringTransferCreate] | None = None,
    overflow_sinking_fund_id: int | None = None,
) -> tuple[IncomeAllocation, bool]:
    """Create or update the single IncomeAllocation row.

    Returns (allocation, created) where created is True if a new row was inserted.
    """
    existing = db.query(IncomeAllocation).first()
    created = existing is None

    if existing is None:
        allocation = IncomeAllocation(
            monthly_income_amount=monthly_income_amount,
            monthly_budget_allocation=monthly_budget_allocation,
            bills_fund_allocation_type=bills_fund_allocation_type,
            bills_fund_fixed_amount=bills_fund_fixed_amount,
            overflow_sinking_fund_id=overflow_sinking_fund_id,
        )
        db.add(allocation)
        db.flush()
    else:
        allocation = existing
        allocation.monthly_income_amount = float(monthly_income_amount)
        allocation.monthly_budget_allocation = float(monthly_budget_allocation)
        allocation.bills_fund_allocation_type = bills_fund_allocation_type
        allocation.bills_fund_fixed_amount = (
            float(bills_fund_fixed_amount)
            if bills_fund_fixed_amount is not None
            else None
        )
        allocation.overflow_sinking_fund_id = overflow_sinking_fund_id
        # Delete existing junction rows
        db.query(IncomeAllocationToSinkingFund).filter(
            IncomeAllocationToSinkingFund.income_allocation_id == allocation.id
        ).delete()

    # Insert new junction rows
    for fa in fund_allocations:
        junction = IncomeAllocationToSinkingFund(
            income_allocation_id=allocation.id,
            sinking_fund_id=fa["sinking_fund_id"],
            allocation_amount=fa["allocation_amount"],
        )
        db.add(junction)

    # Replace recurring transfers
    allocation.recurring_transfers = [
        IncomeAllocationRecurringTransfer(
            description=t.description,
            amount=float(t.amount),
        )
        for t in (recurring_transfers or [])
    ]

    db.commit()
    db.refresh(allocation)
    return allocation, created


@router.get("/income", response_class=HTMLResponse)
async def income_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    allocation = db.query(IncomeAllocation).first()
    sinking_funds = db.query(SinkingFund).filter(SinkingFund.is_deleted == False).all()

    fund_allocation_map: dict[int, float] = {}
    if allocation:
        for junction in allocation.sinking_fund_allocations:
            fund_allocation_map[junction.sinking_fund_id] = junction.allocation_amount

    sinking_funds_data = [
        {"id": f.id, "name": f.name, "color": f.color} for f in sinking_funds
    ]

    recurring_transfers = allocation.recurring_transfers if allocation else []

    return templates.TemplateResponse(
        request,
        "income.html",
        {
            "username": user.username,
            "allocation": allocation,
            "sinking_funds": sinking_funds,
            "fund_allocation_map": fund_allocation_map,
            "sinking_funds_data": sinking_funds_data,
            "recurring_transfers": recurring_transfers,
        },
    )


@router.post("/income", response_class=HTMLResponse)
async def income_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    # Parse income amount
    try:
        monthly_income_amount = Decimal(str(form.get("monthly_income_amount") or "0"))
    except InvalidOperation, TypeError:
        monthly_income_amount = Decimal(0)

    if monthly_income_amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Income must be greater than zero.</p>'
        )

    # Parse budget allocation
    try:
        monthly_budget_allocation = Decimal(
            str(form.get("monthly_budget_allocation") or "0")
        )
    except InvalidOperation, TypeError:
        monthly_budget_allocation = Decimal(0)

    # Parse bills fund allocation type
    bills_fund_allocation_type = str(
        form.get("bills_fund_allocation_type") or "recommended"
    )
    bills_fund_fixed_amount = None

    if bills_fund_allocation_type == "fixed":
        raw = str(form.get("bills_fund_fixed_amount") or "")
        if not raw:
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Fixed amount is required when type is fixed.</p>'
            )
        try:
            bills_fund_fixed_amount = Decimal(raw)
        except InvalidOperation, TypeError:
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Fixed amount is required when type is fixed.</p>'
            )

    # Parse sinking fund allocations from fund_<id> keys (skip system funds — Bills is handled separately)
    system_fund_ids = {
        row.id for row in db.query(SinkingFund.id).filter(SinkingFund.is_system == True)
    }
    fund_allocations = []
    for key in form:
        if key.startswith("fund_"):
            try:
                fund_id = int(key.removeprefix("fund_"))
                if fund_id in system_fund_ids:
                    continue
                amount = Decimal(str(form[key]))
                if amount > 0:
                    fund_allocations.append(
                        {"sinking_fund_id": fund_id, "allocation_amount": amount}
                    )
            except ValueError, InvalidOperation:
                continue

    # Parse recurring transfers from transfer_description_N / transfer_amount_N keys
    transfers: list[IncomeAllocationRecurringTransferCreate] = []
    for key in form:
        if key.startswith("transfer_description_"):
            idx = key.removeprefix("transfer_description_")
            desc = str(form.get(key, "")).strip()
            amt_str = str(form.get(f"transfer_amount_{idx}", "0")).strip()
            try:
                amt = Decimal(amt_str or "0")
            except InvalidOperation:
                amt = Decimal(0)
            if desc and amt > 0:
                transfers.append(
                    IncomeAllocationRecurringTransferCreate(
                        description=desc, amount=amt
                    )
                )

    raw_overflow = form.get("overflow_sinking_fund_id")
    overflow_sinking_fund_id: int | None = None
    if raw_overflow:
        try:
            overflow_sinking_fund_id = int(str(raw_overflow))
        except ValueError:
            pass

    _upsert_allocation(
        db,
        monthly_income_amount,
        monthly_budget_allocation,
        bills_fund_allocation_type,
        bills_fund_fixed_amount,
        fund_allocations,
        transfers,
        overflow_sinking_fund_id,
    )

    return HTMLResponse(
        '<p class="text-green-600 text-sm">Income allocation saved successfully.</p>'
    )


@router.get("/api/income")
async def api_get_income(request: Request, db: Session = Depends(get_db)):
    allocation = db.query(IncomeAllocation).first()
    if not allocation:
        return JSONResponse({"detail": "No income allocation found"}, status_code=404)
    return IncomeAllocationResponse.model_validate(allocation)


@router.post("/api/income")
async def api_post_income(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = IncomeAllocationCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    fund_allocations = [
        {
            "sinking_fund_id": fa.sinking_fund_id,
            "allocation_amount": fa.allocation_amount,
        }
        for fa in data.sinking_fund_allocations
    ]

    bills_fixed = data.bills_fund_fixed_amount
    if data.bills_fund_allocation_type.value == "recommended":
        bills_fixed = None

    allocation, created = _upsert_allocation(
        db,
        data.monthly_income_amount,
        data.monthly_budget_allocation,
        data.bills_fund_allocation_type.value,
        bills_fixed,
        fund_allocations,
        data.recurring_transfers,
        data.overflow_sinking_fund_id,
    )

    response = IncomeAllocationResponse.model_validate(allocation)
    status_code = 201 if created else 200
    return JSONResponse(response.model_dump(mode="json"), status_code=status_code)


# ---------------------------------------------------------------------------
# Secondary income helpers
# ---------------------------------------------------------------------------


def _upsert_secondary_allocation(
    db: Session,
    label: str,
    rules: list[dict],
    overflow_sinking_fund_id: int | None = None,
) -> tuple[SecondaryIncomeAllocation, bool]:
    existing = db.query(SecondaryIncomeAllocation).first()
    created = existing is None

    if existing is None:
        alloc = SecondaryIncomeAllocation(label=label)
        db.add(alloc)
        db.flush()
    else:
        alloc = existing
        alloc.label = label
        db.query(SecondaryIncomeAllocationRule).filter(
            SecondaryIncomeAllocationRule.secondary_income_allocation_id == alloc.id
        ).delete()

    alloc.overflow_sinking_fund_id = overflow_sinking_fund_id

    for r in rules:
        db.add(
            SecondaryIncomeAllocationRule(
                secondary_income_allocation_id=alloc.id,
                sinking_fund_id=r["sinking_fund_id"],
                goal_amount=float(r["goal_amount"]),
                sort_order=r.get("sort_order", 0),
            )
        )

    db.commit()
    db.refresh(alloc)
    return alloc, created


def _record_secondary_income(
    db: Session,
    amount: Decimal,
    date_str: str,
    description: str | None,
) -> list[dict]:
    """Create income + allocation transactions for the secondary income source.

    Funds goals in priority order (sort_order ascending). Surplus after all goals
    are met goes to the overflow sinking fund, or to MonthlyUnallocatedIncome if none.

    Returns a list of {fund_name, amount} dicts describing distributions made.
    """
    alloc = db.query(SecondaryIncomeAllocation).first()
    if not alloc:
        raise ValueError("No secondary income allocation configured")

    income_cat = (
        db.query(Category)
        .filter(Category.type == "income", Category.is_deleted == False)
        .first()
    )
    if not income_cat:
        raise ValueError("No income category found")

    transfer_cat = (
        db.query(Category)
        .filter(Category.type == "transfer", Category.is_deleted == False)
        .first()
    )
    if not transfer_cat:
        raise ValueError("No transfer category found")

    label = alloc.label
    income_desc = description or label

    db.add(
        Transaction(
            date=date_str,
            description=income_desc,
            amount=float(amount),
            category_id=income_cat.id,
            type="income",
            transaction_type="regular",
        )
    )

    remaining = amount
    distributions: list[dict] = []
    year_month = date_str[:7]  # "YYYY-MM" — goal progress resets each calendar month

    for rule in sorted(alloc.rules, key=lambda r: r.sort_order):
        if remaining <= 0:
            break
        fund = (
            db.query(SinkingFund)
            .filter(
                SinkingFund.id == rule.sinking_fund_id,
                SinkingFund.is_deleted == False,
            )
            .first()
        )
        if not fund:
            continue

        already_this_month = Decimal(
            str(
                db.query(func.coalesce(func.sum(Transaction.amount), 0))
                .filter(
                    Transaction.transaction_type == "secondary_income_allocation",
                    Transaction.sinking_fund_id == rule.sinking_fund_id,
                    Transaction.date.like(f"{year_month}%"),
                )
                .scalar()
            )
        )
        goal_remaining = Decimal(str(rule.goal_amount)) - already_this_month
        if goal_remaining <= 0:
            continue

        alloc_amount = min(remaining, goal_remaining).quantize(Decimal("0.01"))
        if alloc_amount <= 0:
            continue

        db.add(
            Transaction(
                date=date_str,
                description=f"{label} allocation to {fund.name}",
                amount=float(alloc_amount),
                category_id=transfer_cat.id,
                type="transfer",
                transaction_type="secondary_income_allocation",
                sinking_fund_id=fund.id,
            )
        )
        fund.current_balance = float(Decimal(str(fund.current_balance)) + alloc_amount)
        remaining -= alloc_amount
        distributions.append({"fund_name": fund.name, "amount": float(alloc_amount)})

    # Send surplus to overflow fund, or track as unallocated
    if remaining > Decimal(0):
        if alloc.overflow_sinking_fund_id:
            overflow_fund = (
                db.query(SinkingFund)
                .filter(
                    SinkingFund.id == alloc.overflow_sinking_fund_id,
                    SinkingFund.is_deleted == False,
                )
                .first()
            )
            if overflow_fund:
                db.add(
                    Transaction(
                        date=date_str,
                        description=f"{label} surplus to {overflow_fund.name}",
                        amount=float(remaining),
                        category_id=transfer_cat.id,
                        type="transfer",
                        transaction_type="income_allocation",
                        sinking_fund_id=overflow_fund.id,
                    )
                )
                overflow_fund.current_balance = float(
                    Decimal(str(overflow_fund.current_balance)) + remaining
                )
                distributions.append(
                    {"fund_name": overflow_fund.name, "amount": float(remaining)}
                )
                remaining = Decimal(0)

        if remaining > Decimal(0):
            now = datetime.now(TIMEZONE)
            month, year = now.month, now.year
            existing_unalloc = (
                db.query(MonthlyUnallocatedIncome)
                .filter(
                    MonthlyUnallocatedIncome.month == month,
                    MonthlyUnallocatedIncome.year == year,
                )
                .first()
            )
            if existing_unalloc:
                existing_unalloc.unallocated_amount = float(
                    Decimal(str(existing_unalloc.unallocated_amount)) + remaining
                )
            else:
                db.add(
                    MonthlyUnallocatedIncome(
                        month=month,
                        year=year,
                        unallocated_amount=float(remaining),
                    )
                )

    db.commit()
    return distributions


# ---------------------------------------------------------------------------
# Secondary income routes — HTML
# ---------------------------------------------------------------------------


@router.get("/income/secondary", response_class=HTMLResponse)
async def secondary_income_page(request: Request, db: Session = Depends(get_db)):
    user = get_current_user(request)
    alloc = db.query(SecondaryIncomeAllocation).first()
    sinking_funds = db.query(SinkingFund).filter(SinkingFund.is_deleted == False).all()
    rule_map: dict[int, dict] = {}
    goal_progress: dict[int, float] = {}
    if alloc:
        for rule in alloc.rules:
            rule_map[rule.sinking_fund_id] = {
                "goal_amount": rule.goal_amount,
                "sort_order": rule.sort_order,
            }
        now = datetime.now(TIMEZONE)
        year_month = f"{now.year}-{now.month:02d}"
        for rule in alloc.rules:
            contributed = (
                db.query(func.coalesce(func.sum(Transaction.amount), 0))
                .filter(
                    Transaction.transaction_type == "secondary_income_allocation",
                    Transaction.sinking_fund_id == rule.sinking_fund_id,
                    Transaction.date.like(f"{year_month}%"),
                )
                .scalar()
            )
            goal_progress[rule.sinking_fund_id] = float(contributed)

    return templates.TemplateResponse(
        request,
        "income_secondary.html",
        {
            "username": user.username,
            "allocation": alloc,
            "sinking_funds": sinking_funds,
            "rule_map": rule_map,
            "goal_progress": goal_progress,
        },
    )


@router.post("/income/secondary", response_class=HTMLResponse)
async def secondary_income_save(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    label = str(form.get("label") or "Secondary Income").strip() or "Secondary Income"

    raw_overflow = form.get("overflow_sinking_fund_id")
    overflow_sinking_fund_id: int | None = None
    if raw_overflow:
        try:
            overflow_sinking_fund_id = int(str(raw_overflow))
        except ValueError:
            pass

    fund_goals: dict[int, Decimal] = {}
    fund_orders: dict[int, int] = {}
    for key in form:
        if key.startswith("sec_goal_"):
            try:
                fund_id = int(key.removeprefix("sec_goal_"))
                goal = Decimal(str(form[key]))
                if goal > 0:
                    fund_goals[fund_id] = goal
            except ValueError, InvalidOperation:
                continue
        elif key.startswith("sec_order_"):
            try:
                fund_id = int(key.removeprefix("sec_order_"))
                order_val = str(form[key]).strip()
                if order_val:
                    fund_orders[fund_id] = int(order_val)
            except ValueError:
                continue

    rules = [
        {
            "sinking_fund_id": fund_id,
            "goal_amount": goal,
            "sort_order": fund_orders.get(fund_id, 0),
        }
        for fund_id, goal in fund_goals.items()
    ]

    _upsert_secondary_allocation(db, label, rules, overflow_sinking_fund_id)
    return HTMLResponse(
        '<p class="text-green-600 text-sm">Secondary income goals saved.</p>'
    )


@router.post("/income/secondary/record", response_class=HTMLResponse)
async def secondary_income_record(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation:
        amount = Decimal(0)

    if amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Amount must be greater than zero.</p>'
        )

    date_str = str(form.get("date") or "").strip()
    if not date_str:
        date_str = datetime.now(TIMEZONE).date().isoformat()

    description = str(form.get("description") or "").strip() or None

    try:
        distributions = _record_secondary_income(db, amount, date_str, description)
    except ValueError as exc:
        return HTMLResponse(f'<p class="text-red-600 text-sm">{exc}</p>')

    if not distributions:
        return HTMLResponse(
            '<p class="text-green-600 text-sm">Income recorded with no fund distributions configured.</p>'
        )

    rows = "".join(
        f"<li>{d['fund_name']}: ${d['amount']:.2f}</li>" for d in distributions
    )
    return HTMLResponse(
        f'<div class="text-green-600 text-sm">'
        f"<p>Income recorded. Distributed:</p><ul>{rows}</ul>"
        f"</div>"
    )


# ---------------------------------------------------------------------------
# Secondary income routes — JSON API
# ---------------------------------------------------------------------------


@router.get("/api/income/secondary")
async def api_get_secondary_income(request: Request, db: Session = Depends(get_db)):
    alloc = db.query(SecondaryIncomeAllocation).first()
    if not alloc:
        return JSONResponse(
            {"detail": "No secondary income allocation found"}, status_code=404
        )
    return JSONResponse(
        SecondaryIncomeAllocationResponse.model_validate(alloc).model_dump(mode="json")
    )


@router.post("/api/income/secondary")
async def api_post_secondary_income(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = SecondaryIncomeAllocationCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    rules = [
        {
            "sinking_fund_id": r.sinking_fund_id,
            "goal_amount": r.goal_amount,
            "sort_order": r.sort_order,
        }
        for r in data.rules
    ]
    alloc, created = _upsert_secondary_allocation(
        db, data.label, rules, data.overflow_sinking_fund_id
    )
    response = SecondaryIncomeAllocationResponse.model_validate(alloc)
    status_code = 201 if created else 200
    return JSONResponse(response.model_dump(mode="json"), status_code=status_code)


@router.post("/api/income/secondary/record")
async def api_record_secondary_income(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = RecordSecondaryIncomeRequest(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    try:
        distributions = _record_secondary_income(
            db, data.amount, data.date, data.description
        )
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return JSONResponse({"distributions": distributions}, status_code=201)
