import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.audit import (
    audit_write,
    column_changes,
    emit_rest,
    related_rows,
    snapshot_columns,
    summary_for,
)
from app.database import get_db
from app.middleware import get_current_user
from app.models import Category, RecurringBill, SinkingFund
from app.schemas import (
    FREQUENCY_LABELS,
    BillFrequency,
    DuePaymentResponse,
    RecurringBillConfirm,
    RecurringBillCreate,
    RecurringBillPay,
    RecurringBillSkip,
    RecurringBillUpdate,
    UnknownFrequencyError,
    annual_multiplier,
)
from app.services.contracts import ContractError, assign_contract, contract_choices
from app.services.payment_methods import (
    PaymentMethodError,
    assign_payment_method,
    paid_with_for,
    payment_method_choices,
)
from app.services.recurring_payments import (
    PaidFromError,
    PaymentBlocked,
    PaymentConflict,
    apply_paid_from,
    cas_save_bill,
    confirm_payment,
    confirm_transaction_payload,
    due_to_confirm,
    find_bills_fund,
    pay_bills_fund,
    pending_bill_values,
    serialize_bill,
    skip_payment,
    update_bill_fields,
)
from app.templating import templates

router = APIRouter()

_VALID_FREQUENCIES = {item.value for item in BillFrequency}


def _active_bills(db: Session):
    return (
        db.query(RecurringBill)
        .filter(RecurringBill.is_active == True)
        .order_by(RecurringBill.next_due_date)
        .all()
    )


def _expense_categories(db: Session):
    return (
        db.query(Category)
        .filter(Category.is_deleted == False, Category.type == "expense")
        .order_by(Category.name)
        .all()
    )


def _compute_annual_cost(amount, frequency: str) -> Decimal:
    return Decimal(str(amount)) * annual_multiplier(frequency)


def _bills_category(db: Session) -> Category | None:
    return (
        db.query(Category)
        .filter(
            Category.name == "Bills",
            Category.type == "expense",
            Category.is_deleted == False,
        )
        .first()
    )


def _budget_categories(db: Session):
    return (
        db.query(Category)
        .filter(
            Category.is_deleted == False,
            Category.type == "expense",
            Category.is_budget_category == True,
        )
        .order_by(Category.name)
        .all()
    )


def _payable_funds(db: Session):
    return (
        db.query(SinkingFund)
        .filter(SinkingFund.is_deleted == False, SinkingFund.name != "Bills")
        .order_by(SinkingFund.name)
        .all()
    )


def _summary(bills: list[RecurringBill]) -> dict[str, Decimal]:
    bills_annual = sum(
        (
            _compute_annual_cost(bill.amount, bill.frequency)
            for bill in bills
            if bill.paid_from == "bills_fund"
        ),
        Decimal(0),
    )
    other_annual = sum(
        (
            _compute_annual_cost(bill.amount, bill.frequency)
            for bill in bills
            if bill.paid_from != "bills_fund"
        ),
        Decimal(0),
    )
    return {
        "total_annual": bills_annual.quantize(Decimal("0.01")),
        "total_monthly": (bills_annual / 12).quantize(Decimal("0.01")),
        "other_monthly": (other_annual / 12).quantize(Decimal("0.01")),
    }


def _bill_context(db: Session) -> dict[str, Any]:
    bills = _active_bills(db)
    categories = _expense_categories(db)
    bills_category = _bills_category(db)
    bills_fund = find_bills_fund(db)
    return {
        "bills": bills,
        "categories": categories,
        "budget_categories": _budget_categories(db),
        "payable_funds": _payable_funds(db),
        "bills_category_id": bills_category.id if bills_category else None,
        "frequency_labels": FREQUENCY_LABELS,
        "due_payments": due_to_confirm(db),
        "due_from": "bills",
        **_summary(bills),
        "bills_fund_id": bills_fund.id if bills_fund else None,
        "paid_with": paid_with_for(db, bills),
        "unset_count": sum(1 for bill in bills if bill.payment_method_id is None),
        "contracts": contract_choices(db, None),
    }


def _filter_bills(bills: list[RecurringBill], payment_method: str | None):
    if not payment_method:
        return bills
    if payment_method == "none":
        return [bill for bill in bills if bill.payment_method_id is None]
    try:
        method_id = int(payment_method)
    except ValueError:
        return bills
    return [bill for bill in bills if bill.payment_method_id == method_id]


def _render_table_body(request: Request, db: Session, ctx: dict | None = None) -> str:
    context = _bill_context(db) if ctx is None else ctx
    return bytes(
        templates.TemplateResponse(
            request,
            "bills.html",
            {**context, "fragment": "table_body"},
        ).body
    ).decode()


def _render_bill_row(request: Request, db: Session, bill: RecurringBill) -> str:
    return bytes(
        templates.TemplateResponse(
            request,
            "bills.html",
            {
                "bill": bill,
                "frequency_labels": FREQUENCY_LABELS,
                "paid_with": paid_with_for(db, [bill]),
                "fragment": "bill_row",
            },
        ).body
    ).decode()


def _choice_context(db: Session) -> dict[str, Any]:
    bills_category = _bills_category(db)
    return {
        "categories": _expense_categories(db),
        "budget_categories": _budget_categories(db),
        "payable_funds": _payable_funds(db),
        "bills_category_id": bills_category.id if bills_category else None,
        "frequency_labels": FREQUENCY_LABELS,
    }


def _render_edit_row(
    request: Request, db: Session, bill: RecurringBill, categories
) -> str:
    return bytes(
        templates.TemplateResponse(
            request,
            "bills.html",
            {
                "bill": bill,
                "categories": categories,
                "frequency_labels": FREQUENCY_LABELS,
                "payment_methods": payment_method_choices(db, bill.payment_method_id),
                "contracts": contract_choices(db, bill.contract_id),
                "budget_categories": _budget_categories(db),
                "payable_funds": _payable_funds(db),
                "bills_category_id": (
                    bills_category.id
                    if (bills_category := _bills_category(db)) is not None
                    else None
                ),
                "fragment": "edit_row",
            },
        ).body
    ).decode()


def _render_confirm_row(
    request: Request, db: Session, bill: RecurringBill, origin: str
) -> str:
    return bytes(
        templates.TemplateResponse(
            request,
            "bills.html",
            {
                "bill": bill,
                "frequency_labels": FREQUENCY_LABELS,
                "paid_with": paid_with_for(db, [bill]),
                "due_from": origin,
                "fragment": "confirm_row",
            },
        ).body
    ).decode()


def _render_due_card(
    request: Request,
    db: Session,
    origin: str,
    message: str | None = None,
    *,
    fragment: str = "card",
    bill: RecurringBill | None = None,
) -> str:
    return bytes(
        templates.TemplateResponse(
            request,
            "_due_to_confirm.html",
            {
                "due_payments": due_to_confirm(db),
                "due_from": origin,
                "message": message,
                "fragment": fragment,
                "bill": bill,
                **_choice_context(db),
            },
        ).body
    ).decode()


def _html_error(exc: PaymentBlocked | PaymentConflict | PaidFromError) -> HTMLResponse:
    if isinstance(exc, PaidFromError):
        detail, status = exc.message, 400
    else:
        detail = exc.detail
        status = 400 if isinstance(exc, PaymentBlocked) else 409
    response = HTMLResponse(
        f'<p class="text-red-600 text-sm">{detail}</p>', status_code=status
    )
    response.headers["HX-Trigger"] = json.dumps({"gwError": detail})
    return response


def _summary_trigger(ctx: dict[str, Any], message: str) -> str:
    return json.dumps(
        {
            "gwBillSuccess": {
                "monthly": str(ctx["total_monthly"]),
                "annual": str(ctx["total_annual"]),
                "other": str(ctx["other_monthly"]),
                "message": message,
            }
        }
    )


def _payment_json(exc: PaymentBlocked | PaymentConflict) -> JSONResponse:
    status = 400 if isinstance(exc, PaymentBlocked) else 409
    return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=status)


def _unknown_frequency_html(exc: UnknownFrequencyError) -> HTMLResponse:
    detail = str(exc)
    response = HTMLResponse(
        f'<p class="text-red-600 text-sm">{detail}</p>', status_code=400
    )
    response.headers["HX-Trigger"] = json.dumps({"gwError": detail})
    return response


def _unknown_frequency_json(exc: UnknownFrequencyError) -> JSONResponse:
    return JSONResponse(
        {"detail": str(exc), "code": "unknown_frequency"}, status_code=400
    )


def _oob(html: str, swap: str) -> str:
    stripped = html.lstrip()
    if stripped.startswith("<tr "):
        return stripped.replace("<tr ", f'<tr hx-swap-oob="{swap}" ', 1)
    if 'id="due-to-confirm"' in stripped:
        return stripped.replace(
            'id="due-to-confirm"',
            f'id="due-to-confirm" hx-swap-oob="{swap}"',
            1,
        )
    return html


def _after_payment(
    request: Request, db: Session, bill: RecurringBill, origin: str, message: str
) -> HTMLResponse:
    """Return the row or the Due to confirm card, and keep the other in step."""
    row = _render_bill_row(request, db, bill)
    card = _render_due_card(
        request, db, "dashboard" if origin == "dashboard" else "bills"
    )
    if origin == "dashboard":
        body = card
    elif origin == "card":
        extra = _oob(row, "outerHTML")
        body = card + extra
    else:
        if card.strip():
            extra = _oob(card, "outerHTML")
        else:
            extra = '<div id="due-to-confirm" hx-swap-oob="delete"></div>'
        body = row + extra
    response = HTMLResponse(body)
    response.headers["HX-Trigger-After-Swap"] = json.dumps(
        {"gwBillSuccess": {"message": message}}
    )
    return response


def _resolve_paid_from_form(db: Session, raw: str, form) -> tuple[str, int | None, int]:
    if raw == "bills_fund":
        bills_category = _bills_category(db)
        if bills_category is None:
            raise PaidFromError("Bills category not found. Please create it first.")
        return "bills_fund", None, bills_category.id
    if raw.startswith("budget:"):
        return "budget", None, int(raw.split(":", 1)[1])
    if raw.startswith("fund:"):
        fund_id = int(raw.split(":", 1)[1])
        category_raw = str(form.get("category_id") or "").strip()
        if not category_raw:
            raise PaidFromError("Choose an expense category.")
        return "sinking_fund", fund_id, int(category_raw)
    raise PaidFromError("Choose where this is paid from.")


# ---------------------------------------------------------------------------
# HTML routes
# ---------------------------------------------------------------------------


@router.get("/bills", response_class=HTMLResponse)
async def bills_page(
    request: Request,
    payment_method: str | None = None,
    db: Session = Depends(get_db),
):
    user = get_current_user(request)
    ctx = _bill_context(db)
    ctx["payment_filter"] = payment_method
    ctx["bills"] = _filter_bills(ctx["bills"], payment_method)
    return templates.TemplateResponse(
        request,
        "bills.html",
        {"username": user.username, **ctx},
    )


@router.post("/bills", response_class=HTMLResponse)
async def bills_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()

    name = str(form.get("name") or "").strip()
    provider = str(form.get("debtor_provider") or "").strip()
    frequency = str(form.get("frequency") or "")
    start_date = str(form.get("start_date") or "").strip()
    next_due_date = str(form.get("next_due_date") or "").strip()
    bill_type = str(form.get("bill_type") or "fixed").strip()

    if not name or not provider:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Name and provider are required.</p>'
        )

    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation, TypeError:
        return HTMLResponse('<p class="text-red-600 text-sm">Invalid amount.</p>')

    if amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Amount must be greater than zero.</p>'
        )

    if frequency not in _VALID_FREQUENCIES:
        return HTMLResponse('<p class="text-red-600 text-sm">Choose a frequency.</p>')

    if not start_date or not next_due_date:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Start date and next due date are required.</p>'
        )
    try:
        date.fromisoformat(start_date)
        date.fromisoformat(next_due_date)
    except ValueError:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Enter a real start date and next due date.</p>',
            status_code=400,
        )

    if bill_type not in ("fixed", "variable"):
        bill_type = "fixed"

    try:
        paid_from, fund_id, category_id = _resolve_paid_from_form(
            db, str(form.get("paid_from") or "bills_fund"), form
        )
    except PaidFromError as exc:
        return _html_error(exc)
    except ValueError:
        return _html_error(PaidFromError("Choose where this is paid from."))

    foreign_amount_raw = form.get("foreign_amount")
    foreign_currency_raw = (
        str(form.get("foreign_currency") or "").strip().upper() or None
    )
    foreign_amount = None
    if foreign_amount_raw:
        try:
            fa = Decimal(str(foreign_amount_raw))
            foreign_amount = fa if fa > 0 else None
        except InvalidOperation, TypeError:
            pass

    bill = RecurringBill(
        name=name,
        amount=amount,
        debtor_provider=provider,
        start_date=start_date,
        frequency=frequency,
        category_id=category_id,
        next_due_date=next_due_date,
        bill_type=bill_type,
        foreign_amount=foreign_amount,
        foreign_currency=foreign_currency_raw if foreign_amount else None,
    )
    try:
        apply_paid_from(
            db,
            bill,
            paid_from=paid_from,
            paid_from_fund_id=fund_id,
            category_id=category_id,
            force=True,
        )
    except PaidFromError as exc:
        db.rollback()
        return _html_error(exc)
    db.add(bill)
    if "contract_id" in form:
        raw_contract = str(form.get("contract_id") or "").strip()
        try:
            contract_id = int(raw_contract) if raw_contract else None
        except ValueError:
            db.rollback()
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Contract not found or ended.</p>',
                status_code=422,
            )
        try:
            assign_contract(db, bill, contract_id)
        except ContractError as exc:
            db.rollback()
            return HTMLResponse(
                f'<p class="text-red-600 text-sm">{exc.message}</p>',
                status_code=exc.status,
            )
    db.commit()

    ctx = _bill_context(db)
    response = HTMLResponse(_render_table_body(request, db, ctx))
    response.headers["HX-Trigger-After-Swap"] = _summary_trigger(ctx, "Bill added.")
    return response


@router.get("/bills/due-card", response_class=HTMLResponse)
async def bills_due_card(
    request: Request,
    origin: str = Query("bills", alias="from"),
    db: Session = Depends(get_db),
):
    return HTMLResponse(_render_due_card(request, db, origin))


@router.get("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_get_row(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(_render_bill_row(request, db, bill))


@router.get("/bills/{bill_id}/edit", response_class=HTMLResponse)
async def bills_edit_form(
    request: Request, bill_id: int, db: Session = Depends(get_db)
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    categories = _expense_categories(db)
    return HTMLResponse(_render_edit_row(request, db, bill, categories))


@router.get("/bills/{bill_id}/confirm", response_class=HTMLResponse)
async def bills_confirm_form(
    request: Request,
    bill_id: int,
    origin: str = Query("bills", alias="from"),
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    if origin == "dashboard":
        return HTMLResponse(
            _render_due_card(request, db, origin, fragment="form", bill=bill)
        )
    return HTMLResponse(_render_confirm_row(request, db, bill, origin))


@router.post("/bills/{bill_id}/confirm", response_class=HTMLResponse)
async def bills_confirm(
    request: Request,
    bill_id: int,
    origin: str = Query("bills", alias="from"),
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation, TypeError:
        return HTMLResponse('<p class="text-red-600 text-sm">Invalid amount.</p>')
    if amount <= 0:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Amount must be greater than zero.</p>'
        )
    due_date = str(form.get("due_date") or "").strip()
    try:
        result = confirm_payment(db, bill, amount, due_date)
        db.commit()
        db.refresh(result.bill)
    except UnknownFrequencyError as exc:
        db.rollback()
        return _unknown_frequency_html(exc)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return _html_error(exc)
    except ValueError:
        db.rollback()
        return HTMLResponse('<p class="text-red-600 text-sm">Invalid due date.</p>')
    return _after_payment(request, db, result.bill, origin, result.message)


@router.post("/bills/{bill_id}/skip", response_class=HTMLResponse)
async def bills_skip(
    request: Request,
    bill_id: int,
    origin: str = Query("bills", alias="from"),
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    due_date = str(form.get("due_date") or "").strip()
    try:
        result = skip_payment(db, bill, due_date)
        db.commit()
        db.refresh(result.bill)
    except UnknownFrequencyError as exc:
        db.rollback()
        return _unknown_frequency_html(exc)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return _html_error(exc)
    except ValueError:
        db.rollback()
        return HTMLResponse('<p class="text-red-600 text-sm">Invalid due date.</p>')
    return _after_payment(request, db, result.bill, origin, result.message)


@router.post("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_update(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)

    before = (
        Decimal(str(bill.amount)),
        bill.frequency,
        bill.paid_from,
        bill.paid_from_fund_id,
        bill.is_active,
    )
    form = await request.form()

    next_due_raw = str(form.get("next_due_date") or "").strip()
    was_raw = str(form.get("next_due_date_was") or "").strip()
    try:
        next_due = date.fromisoformat(next_due_raw).isoformat()
        expected_due = date.fromisoformat(was_raw).isoformat()
    except ValueError:
        return HTMLResponse(
            '<p class="text-red-600 text-sm">Enter a real next due date.</p>',
            status_code=400,
        )

    name = str(form.get("name") or "").strip()
    provider = str(form.get("debtor_provider") or "").strip()

    if name:
        bill.name = name
    if provider:
        bill.debtor_provider = provider

    raw_amount = form.get("amount")
    if raw_amount:
        try:
            amount = Decimal(str(raw_amount))
            if amount > 0:
                bill.amount = float(amount)
        except InvalidOperation, TypeError:
            pass

    frequency = form.get("frequency")
    if frequency:
        if str(frequency) not in _VALID_FREQUENCIES:
            db.rollback()
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Choose a frequency.</p>',
                status_code=400,
            )
        bill.frequency = str(frequency)

    bill_type = form.get("bill_type")
    if bill_type in ("fixed", "variable"):
        bill.bill_type = str(bill_type)

    foreign_amount_raw = form.get("foreign_amount")
    foreign_currency_raw = (
        str(form.get("foreign_currency") or "").strip().upper() or None
    )
    if foreign_amount_raw is not None:
        try:
            fa = Decimal(str(foreign_amount_raw))
            bill.foreign_amount = float(fa) if fa > 0 else None
        except InvalidOperation, TypeError:
            bill.foreign_amount = None
        bill.foreign_currency = foreign_currency_raw if bill.foreign_amount else None

    if "payment_method_id" in form:
        raw = str(form.get("payment_method_id") or "").strip()
        try:
            new_id = int(raw) if raw else None
        except ValueError:
            db.rollback()
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Payment method not found or archived.</p>',
                status_code=422,
            )
        try:
            assign_payment_method(db, bill, new_id)
        except PaymentMethodError as exc:
            db.rollback()
            return HTMLResponse(
                f'<p class="text-red-600 text-sm">{exc.message}</p>',
                status_code=exc.status,
            )

    if "contract_id" in form:
        raw_contract = str(form.get("contract_id") or "").strip()
        try:
            contract_id = int(raw_contract) if raw_contract else None
        except ValueError:
            db.rollback()
            return HTMLResponse(
                '<p class="text-red-600 text-sm">Contract not found or ended.</p>',
                status_code=422,
            )
        try:
            assign_contract(db, bill, contract_id)
        except ContractError as exc:
            db.rollback()
            return HTMLResponse(
                f'<p class="text-red-600 text-sm">{exc.message}</p>',
                status_code=exc.status,
            )

    if "paid_from" in form:
        try:
            paid_from, fund_id, category_id = _resolve_paid_from_form(
                db, str(form.get("paid_from") or ""), form
            )
            apply_paid_from(
                db,
                bill,
                paid_from=paid_from,
                paid_from_fund_id=fund_id,
                category_id=category_id,
            )
        except PaidFromError as exc:
            db.rollback()
            return _html_error(exc)
        except ValueError:
            db.rollback()
            return _html_error(PaidFromError("Choose where this is paid from."))

    values = pending_bill_values(bill)
    values.pop("next_due_date", None)
    values["next_due_date"] = next_due
    try:
        cas_save_bill(db, bill, values, expected_due)
    except PaymentConflict as exc:
        db.rollback()
        return _html_error(exc)

    db.commit()
    db.refresh(bill)

    response = HTMLResponse(_render_bill_row(request, db, bill))
    after = (
        Decimal(str(bill.amount)),
        bill.frequency,
        bill.paid_from,
        bill.paid_from_fund_id,
        bill.is_active,
    )
    if before != after:
        response.headers["HX-Trigger-After-Swap"] = _summary_trigger(
            _bill_context(db), "Saved."
        )
    return response


@router.delete("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_delete(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    bill.is_active = False
    db.commit()
    response = HTMLResponse("")
    response.headers["HX-Trigger-After-Swap"] = _summary_trigger(
        _bill_context(db), "Saved."
    )
    return response


# ---------------------------------------------------------------------------
# JSON API routes
# ---------------------------------------------------------------------------


@router.get("/api/bills")
async def api_list_bills(request: Request, db: Session = Depends(get_db)):
    return [serialize_bill(bill) for bill in _active_bills(db)]


@router.get("/api/bills/due")
async def api_due_bills(request: Request, db: Session = Depends(get_db)):
    return [
        DuePaymentResponse.model_validate(row).model_dump(mode="json")
        for row in due_to_confirm(db)
    ]


@router.post("/api/bills")
@audit_write
async def api_create_bill(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        data = RecurringBillCreate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    bill = RecurringBill(
        name=data.name,
        amount=data.amount,
        debtor_provider=data.debtor_provider,
        start_date=data.start_date,
        frequency=data.frequency.value,
        category_id=data.category_id,
        end_date=data.end_date,
        is_active=data.is_active,
        next_due_date=data.next_due_date,
        bill_type=data.bill_type.value,
        foreign_amount=data.foreign_amount,
        foreign_currency=data.foreign_currency,
    )
    try:
        apply_paid_from(
            db,
            bill,
            paid_from=data.paid_from.value,
            paid_from_fund_id=data.paid_from_fund_id,
            category_id=data.category_id,
            force=True,
        )
    except PaidFromError as exc:
        db.rollback()
        return JSONResponse({"detail": exc.message}, status_code=400)
    db.add(bill)
    db.flush()
    if data.payment_method_id is not None:
        try:
            assign_payment_method(db, bill, data.payment_method_id)
        except PaymentMethodError as exc:
            db.rollback()
            return JSONResponse({"detail": exc.message}, status_code=exc.status)
    if data.contract_id is not None:
        try:
            assign_contract(db, bill, data.contract_id)
        except ContractError as exc:
            db.rollback()
            return JSONResponse({"detail": exc.message}, status_code=exc.status)
    db.commit()
    db.refresh(bill)
    emit_rest(
        request,
        action="create",
        entity_type="bill",
        entity_id=bill.id,
        values=summary_for("bill", snapshot_columns(bill)),
    )

    return JSONResponse(serialize_bill(bill), status_code=201)


@router.get("/api/bills/{bill_id}")
async def api_get_bill(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)
    return JSONResponse(serialize_bill(bill), status_code=200)


@router.put("/api/bills/{bill_id}")
@audit_write
async def api_update_bill(
    request: Request, bill_id: int, db: Session = Depends(get_db)
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)

    try:
        body = await request.json()
        data = RecurringBillUpdate(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    before = snapshot_columns(bill)
    updates = data.model_dump(exclude_unset=True)
    payment_method_set = "payment_method_id" in updates
    payment_method_id = updates.pop("payment_method_id", None)
    contract_set = "contract_id" in updates
    contract_id = updates.pop("contract_id", None)
    if payment_method_set:
        try:
            assign_payment_method(db, bill, payment_method_id)
        except PaymentMethodError as exc:
            db.rollback()
            return JSONResponse({"detail": exc.message}, status_code=exc.status)
    if contract_set:
        try:
            assign_contract(db, bill, contract_id)
        except ContractError as exc:
            db.rollback()
            return JSONResponse({"detail": exc.message}, status_code=exc.status)

    try:
        update_bill_fields(db, bill, data)
    except PaidFromError as exc:
        db.rollback()
        return JSONResponse({"detail": exc.message}, status_code=400)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return _payment_json(exc)

    db.commit()
    db.refresh(bill)
    emit_rest(
        request,
        action="update",
        entity_type="bill",
        entity_id=bill.id,
        changes=column_changes(before, snapshot_columns(bill)),
    )
    return JSONResponse(serialize_bill(bill), status_code=200)


@router.delete("/api/bills/{bill_id}")
@audit_write
async def api_delete_bill(
    request: Request, bill_id: int, db: Session = Depends(get_db)
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)
    values = summary_for("bill", snapshot_columns(bill))
    bill.is_active = False
    db.commit()
    emit_rest(
        request,
        action="delete",
        entity_type="bill",
        entity_id=bill.id,
        values=values,
    )
    return JSONResponse({"detail": "Bill deactivated"}, status_code=200)


@router.post("/api/bills/{bill_id}/pay")
@audit_write
async def api_pay_bill(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)

    try:
        body = await request.json()
        data = RecurringBillPay(**body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            errors = [
                {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ]
            return JSONResponse({"detail": errors}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=422)

    try:
        paid = pay_bills_fund(db, bill, data.amount, data.date)
        after = snapshot_columns(bill)
        payment_id = paid.transaction.id
        db.commit()
        emit_rest(
            request,
            action="pay",
            entity_type="bill",
            entity_id=bill.id,
            changes=column_changes(paid.prior, after),
            related=related_rows("transaction", [payment_id], "create"),
        )
        db.refresh(bill)
        db.refresh(paid.transaction)
    except UnknownFrequencyError as exc:
        db.rollback()
        return _unknown_frequency_json(exc)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return _payment_json(exc)

    return JSONResponse(
        {
            "transaction": confirm_transaction_payload(paid.transaction),
            "bill": serialize_bill(bill),
        },
        status_code=200,
    )


def _validation_error(exc: ValidationError | ValueError) -> JSONResponse:
    if isinstance(exc, ValidationError):
        errors = [
            {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        return JSONResponse({"detail": errors}, status_code=422)
    return JSONResponse({"detail": str(exc)}, status_code=422)


@router.post("/api/bills/{bill_id}/confirm")
@audit_write
async def api_confirm_bill(
    request: Request, bill_id: int, db: Session = Depends(get_db)
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)
    try:
        body = await request.json()
        data = RecurringBillConfirm(**body)
    except (ValidationError, ValueError) as exc:
        return _validation_error(exc)
    try:
        result = confirm_payment(db, bill, data.amount, data.due_date)
        after = snapshot_columns(result.bill)
        related_ids = [result.transaction.id]
        taken = result.taken_back or {}
        clawback_id = taken.get("transaction_id")
        if clawback_id is not None:
            related_ids.append(int(clawback_id))
        db.commit()
        emit_rest(
            request,
            action="confirm",
            entity_type="bill",
            entity_id=bill.id,
            changes=column_changes(result.prior, after),
            related=related_rows("transaction", related_ids, "create"),
        )
        db.refresh(result.transaction)
        db.refresh(result.bill)
    except UnknownFrequencyError as exc:
        db.rollback()
        return _unknown_frequency_json(exc)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return _payment_json(exc)
    return JSONResponse(
        {
            "transaction": confirm_transaction_payload(result.transaction),
            "bill": serialize_bill(result.bill),
            "taken_back": result.taken_back,
            "message": result.message,
        }
    )


@router.post("/api/bills/{bill_id}/skip")
@audit_write
async def api_skip_bill(request: Request, bill_id: int, db: Session = Depends(get_db)):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return JSONResponse({"detail": "Bill not found"}, status_code=404)
    try:
        body = await request.json()
        data = RecurringBillSkip(**body)
    except (ValidationError, ValueError) as exc:
        return _validation_error(exc)
    try:
        result = skip_payment(db, bill, data.due_date)
        after = snapshot_columns(result.bill)
        db.commit()
        emit_rest(
            request,
            action="skip",
            entity_type="bill",
            entity_id=bill.id,
            changes=column_changes(result.prior, after),
        )
        db.refresh(result.bill)
    except UnknownFrequencyError as exc:
        db.rollback()
        return _unknown_frequency_json(exc)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        return _payment_json(exc)
    return JSONResponse(
        {"bill": serialize_bill(result.bill), "message": result.message}
    )
