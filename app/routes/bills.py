import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
    BillFieldError,
    BillFrequency,
    DuePaymentResponse,
    RecurringBillConfirm,
    RecurringBillCreate,
    RecurringBillPay,
    RecurringBillSkip,
    RecurringBillUpdate,
    UnknownFrequencyError,
    annual_multiplier,
    clean_bill_note,
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
    _problem,
    _today,
    apply_paid_from,
    cas_save_bill,
    confirm_payment,
    confirm_transaction_payload,
    due_soon,
    due_to_confirm,
    find_bills_fund,
    pay_bills_fund,
    pending_bill_values,
    serialize_bill,
    skip_payment,
    update_bill_fields,
)
from app.templating import _money_format, templates

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


def _pile_monthly(bills: list[RecurringBill], marker: str | None) -> Decimal:
    """Monthly average for one mark. Sum the year, divide, then quantize once."""
    annual = sum(
        (
            _compute_annual_cost(bill.amount, bill.frequency)
            for bill in bills
            if bill.importance == marker
        ),
        Decimal(0),
    )
    return (annual / 12).quantize(Decimal("0.01"))


def _summary(bills: list[RecurringBill]) -> dict[str, Decimal | int]:
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
        "essential_monthly": _pile_monthly(bills, "essential"),
        "optional_monthly": _pile_monthly(bills, "optional"),
        "unmarked_monthly": _pile_monthly(bills, None),
        "unmarked_count": sum(1 for bill in bills if bill.importance is None),
    }


def _bill_context(db: Session) -> dict[str, Any]:
    bills = _active_bills(db)
    categories = _expense_categories(db)
    bills_category = _bills_category(db)
    bills_fund = find_bills_fund(db)
    today = _today()
    due_rows = due_to_confirm(db, today)
    return {
        "bills": bills,
        "categories": categories,
        "budget_categories": _budget_categories(db),
        "payable_funds": _payable_funds(db),
        "bills_category_id": bills_category.id if bills_category else None,
        "frequency_labels": FREQUENCY_LABELS,
        "due_payments": due_rows,
        "due_from": "bills",
        **_summary(bills),
        "bills_fund_id": bills_fund.id if bills_fund else None,
        "paid_with": paid_with_for(db, bills),
        "unset_count": sum(1 for bill in bills if bill.payment_method_id is None),
        "contracts": contract_choices(db, None),
        "payment_filter": None,
        "importance_filter": None,
        "filter_query": "",
        "query": "",
        "today": today.isoformat(),
        "show_marks": False,
        "active_count": len(bills),
        "waiting_count": len(due_rows),
        "chips": _chip_rows(db, today),
    }


def _filter_bills(
    bills: list[RecurringBill],
    payment_method: str | None,
    importance: str | None = None,
) -> list[RecurringBill]:
    """AND the page filters. An unknown value leaves that filter off."""
    filtered = bills
    if payment_method == "none":
        filtered = [bill for bill in filtered if bill.payment_method_id is None]
    elif payment_method:
        try:
            method_id = int(payment_method)
        except ValueError:
            method_id = None
        if method_id is not None:
            filtered = [
                bill for bill in filtered if bill.payment_method_id == method_id
            ]
    if importance == "unset":
        filtered = [bill for bill in filtered if bill.importance is None]
    elif importance in ("essential", "optional"):
        filtered = [bill for bill in filtered if bill.importance == importance]
    return filtered


def _page_query(
    payment_method: str | None, importance: str | None, **extra: str
) -> str:
    pairs: list[tuple[str, str]] = []
    if payment_method:
        pairs.append(("payment_method", payment_method))
    if importance:
        pairs.append(("importance", importance))
    for key, value in extra.items():
        if value:
            pairs.append((key, value))
    if not pairs:
        return ""
    return "?" + urlencode(pairs)


def _read_importance(value: object) -> str | None:
    """Blank and the word unset mean not set yet. Anything else is refused."""
    text = str(value or "").strip()
    if text in ("", "unset"):
        return None
    if text in ("essential", "optional"):
        return text
    raise BillFieldError(
        "importance",
        "Choose keep paying, safe to cancel, or not set yet.",
    )


def _create_error(message: str, status: int = 422) -> HTMLResponse:
    """The same paragraph for every create failure, with no HX-Trigger."""
    return HTMLResponse(
        f'<p class="text-red-600 text-sm">{message}</p>',
        status_code=status,
    )


def _editor_error(
    message: str,
    status: int,
    *,
    focus: str | None = None,
    retarget: str = "#bill-editor-errors",
    extra: str = "",
) -> HTMLResponse:
    """Drawer or strip error. No HX-Trigger, so the sentence is not shown twice."""
    response = HTMLResponse(
        f'<p class="gw-msg-error">{message}</p>{extra}',
        status_code=status,
    )
    response.headers["HX-Retarget"] = retarget
    response.headers["HX-Reswap"] = "innerHTML"
    if focus:
        response.headers["HX-Focus"] = focus
    return response


def _error_target(origin: str, bill_id: int) -> str:
    if origin == "strip":
        return f"#due-soon-errors-{bill_id}"
    return "#bill-editor-errors"


def _occurrence_error(
    origin: str, bill_id: int, message: str, status: int
) -> HTMLResponse:
    """Dashboard and card keep today's body and status. The bills page retargets."""
    if origin in {"dashboard", "card"}:
        return HTMLResponse(
            f'<p class="text-red-600 text-sm">{message}</p>',
            status_code=status,
        )
    return _editor_error(message, status, retarget=_error_target(origin, bill_id))


def _parse_mark_fields(form) -> tuple[str | None, str | None, str | None]:
    importance = (
        _read_importance(form.get("importance")) if "importance" in form else None
    )
    cancel_note = (
        clean_bill_note(form.get("cancel_note")) if "cancel_note" in form else None
    )
    if_stopped_note = (
        clean_bill_note(form.get("if_stopped_note"))
        if "if_stopped_note" in form
        else None
    )
    return importance, cancel_note, if_stopped_note


def _html(request: Request, name: str, context: dict[str, Any]) -> str:
    return bytes(templates.TemplateResponse(request, name, context).body).decode()


def _chip_rows(db: Session, today: date | None = None) -> list[dict[str, Any]]:
    today = today or _today()
    confirm = {row["bill_id"]: row for row in due_to_confirm(db, today)}
    chips: list[dict[str, Any]] = []
    for bill in due_soon(db, today):
        row = confirm.get(bill.id)
        blocked = _problem(db, bill, date.fromisoformat(bill.next_due_date))
        problem = None
        if blocked is not None:
            problem = {
                "code": blocked.code,
                "detail": blocked.detail,
                "href": blocked.href,
            }
        chips.append(
            {
                "bill": bill,
                "in_confirm": row is not None,
                "more_due": row["more_due"] if row else 0,
                "problem": problem,
            }
        )
    return chips


def _render_bill_list(request: Request, db: Session, ctx: dict | None = None) -> str:
    context = _bill_context(db) if ctx is None else ctx
    return _html(request, "_bill_list.html", context)


def _render_bill_row(
    request: Request,
    db: Session,
    bill: RecurringBill,
    filter_query: str = "",
    *,
    show_marks: bool = False,
) -> str:
    return _html(
        request,
        "_bill_line.html",
        {
            "bill": bill,
            "frequency_labels": FREQUENCY_LABELS,
            "paid_with": paid_with_for(db, [bill]),
            "query": filter_query,
            "today": _today().isoformat(),
            "show_marks": show_marks,
        },
    )


def _render_due_soon(request: Request, db: Session) -> str:
    today = _today()
    return _html(
        request,
        "_due_soon.html",
        {"chips": _chip_rows(db, today), "today": today.isoformat()},
    )


_EMPTY_DRAWER = '<div id="bill-drawer" hx-swap-oob="innerHTML"></div>'


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
    request: Request,
    db: Session,
    bill: RecurringBill,
    categories,
    *,
    filter_query: str = "",
) -> str:
    bills_category = _bills_category(db)
    return _html(
        request,
        "_bill_editor.html",
        {
            "bill": bill,
            "categories": categories,
            "frequency_labels": FREQUENCY_LABELS,
            "payment_methods": payment_method_choices(db, bill.payment_method_id),
            "contracts": contract_choices(db, bill.contract_id),
            "budget_categories": _budget_categories(db),
            "payable_funds": _payable_funds(db),
            "bills_category_id": bills_category.id if bills_category else None,
            "query": filter_query,
            "today": _today().isoformat(),
        },
    )


def _render_confirm_fields(request: Request, bill: RecurringBill) -> str:
    return _html(
        request,
        "_bill_confirm.html",
        {"bill": bill, "confirm_cancel": "row"},
    )


def _render_strip_confirm(request: Request, bill: RecurringBill) -> str:
    return _html(request, "_due_soon_confirm.html", {"bill": bill})


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


def _summary_trigger(
    ctx: dict[str, Any], message: str, bill_id: int | None = None
) -> str:
    payload: dict[str, Any] = {
        "monthly": str(ctx["total_monthly"]),
        "annual": str(ctx["total_annual"]),
        "other": str(ctx["other_monthly"]),
        "essential": _money_format(ctx["essential_monthly"]),
        "optional": _money_format(ctx["optional_monthly"]),
        "unmarked": _money_format(ctx["unmarked_monthly"]),
        "unmarked_count": ctx["unmarked_count"],
        "active_count": ctx["active_count"],
        "waiting_count": ctx["waiting_count"],
        "message": message,
    }
    if bill_id is not None:
        payload["billId"] = bill_id
    return json.dumps({"gwBillSuccess": payload})


def _payment_trigger(db: Session, message: str, bill_id: int) -> str:
    ctx = _bill_context(db)
    return json.dumps(
        {
            "gwBillSuccess": {
                "message": message,
                "billId": bill_id,
                "active_count": ctx["active_count"],
                "waiting_count": ctx["waiting_count"],
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
    if stripped.startswith("<"):
        end = stripped.find(">")
        space = stripped.find(" ")
        if space != -1 and (end == -1 or space < end):
            return stripped[:space] + f' hx-swap-oob="{swap}"' + stripped[space:]
        if end != -1:
            return stripped[:end] + f' hx-swap-oob="{swap}"' + stripped[end:]
    return html


def _list_query(
    payment_method: str | None, importance: str | None, marks: str | None
) -> str:
    extra = {"marks": marks} if marks == "1" else {}
    return _page_query(payment_method, importance, **extra)


def _after_payment(
    request: Request,
    db: Session,
    bill: RecurringBill,
    origin: str,
    message: str,
    *,
    payment_method: str | None = None,
    importance: str | None = None,
    marks: str | None = None,
) -> HTMLResponse:
    """Return the row or the Due to confirm card, and keep the other in step."""
    query = _list_query(payment_method, importance, marks)
    row = _render_bill_row(request, db, bill, query, show_marks=marks == "1")
    if origin == "dashboard":
        body = _render_due_card(request, db, "dashboard")
        trigger = json.dumps({"gwBillSuccess": {"message": message}})
    elif origin == "card":
        card = _render_due_card(request, db, "bills")
        body = card + _oob(row, "outerHTML")
        trigger = json.dumps({"gwBillSuccess": {"message": message}})
    elif origin == "strip":
        body = _render_due_soon(request, db) + _oob(row, "outerHTML")
        trigger = _payment_trigger(db, message, bill.id)
    else:
        strip = "" if marks == "1" else _oob(_render_due_soon(request, db), "outerHTML")
        body = row + strip + _EMPTY_DRAWER
        trigger = _payment_trigger(db, message, bill.id)
    response = HTMLResponse(body)
    response.headers["HX-Trigger-After-Swap"] = trigger
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
    importance: str | None = None,
    db: Session = Depends(get_db),
):
    if importance:
        target = "/bills/if-something-happens"
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(url=target, status_code=303)
    user = get_current_user(request)
    ctx = _bill_context(db)
    ctx["payment_filter"] = payment_method
    ctx["filter_query"] = _page_query(payment_method, None)
    ctx["query"] = ctx["filter_query"]
    ctx["bills"] = _filter_bills(ctx["bills"], payment_method, None)
    visible = {bill.id for bill in ctx["bills"]}
    ctx["chips"] = [chip for chip in ctx["chips"] if chip["bill"].id in visible]
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
        return _create_error("Name and provider are required.")

    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation, TypeError:
        return _create_error("Invalid amount.")

    if amount <= 0:
        return _create_error("Amount must be greater than zero.")

    if frequency not in _VALID_FREQUENCIES:
        return _create_error("Choose a frequency.")

    if not start_date or not next_due_date:
        return _create_error("Start date and next due date are required.")
    try:
        date.fromisoformat(start_date)
        date.fromisoformat(next_due_date)
    except ValueError:
        return _create_error("Enter a real start date and next due date.")

    if bill_type not in ("fixed", "variable"):
        bill_type = "fixed"

    try:
        importance, cancel_note, if_stopped_note = _parse_mark_fields(form)
    except BillFieldError as exc:
        return _create_error(exc.message)

    try:
        paid_from, fund_id, category_id = _resolve_paid_from_form(
            db, str(form.get("paid_from") or "bills_fund"), form
        )
    except PaidFromError as exc:
        return _create_error(exc.message, 400)
    except ValueError:
        return _create_error("Choose where this is paid from.", 400)

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
        importance=importance,
        cancel_note=cancel_note,
        if_stopped_note=if_stopped_note,
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
        return _create_error(exc.message, 400)
    db.add(bill)
    if "contract_id" in form:
        raw_contract = str(form.get("contract_id") or "").strip()
        try:
            contract_id = int(raw_contract) if raw_contract else None
        except ValueError:
            db.rollback()
            return _create_error("Contract not found or ended.")
        try:
            assign_contract(db, bill, contract_id)
        except ContractError as exc:
            db.rollback()
            return _create_error(exc.message, exc.status)
    db.commit()

    ctx = _bill_context(db)
    body = _render_bill_list(request, db, ctx) + _oob(
        _render_due_soon(request, db), "outerHTML"
    )
    response = HTMLResponse(body)
    response.headers["HX-Trigger-After-Swap"] = _summary_trigger(ctx, "Bill added.")
    return response


@router.get("/bills/due-card", response_class=HTMLResponse)
async def bills_due_card(
    request: Request,
    origin: str = Query("bills", alias="from"),
    db: Session = Depends(get_db),
):
    return HTMLResponse(_render_due_card(request, db, origin))


@router.get("/bills/due-soon", response_class=HTMLResponse)
async def bills_due_soon(request: Request, db: Session = Depends(get_db)):
    return HTMLResponse(_render_due_soon(request, db))


@router.get("/bills/if-something-happens", response_class=HTMLResponse)
async def bills_if_something_happens(
    request: Request,
    payment_method: str | None = None,
    importance: str | None = None,
    db: Session = Depends(get_db),
):
    """Quiet mark view. marks=1 rides on the editor so a save skips the strip."""
    user = get_current_user(request)
    ctx = _bill_context(db)
    base = "/bills/if-something-happens"
    ctx["payment_filter"] = payment_method
    ctx["importance_filter"] = importance
    ctx["show_marks"] = True
    ctx["query"] = _list_query(payment_method, importance, "1")
    ctx["filter_query"] = ctx["query"]
    ctx["keep_href"] = base + _page_query(payment_method, "essential")
    ctx["cancel_href"] = base + _page_query(payment_method, "optional")
    ctx["unset_href"] = base + _page_query(payment_method, "unset")
    ctx["bills"] = _filter_bills(ctx["bills"], payment_method, importance)
    return templates.TemplateResponse(
        request,
        "bills_if_something.html",
        {"username": user.username, **ctx},
    )


@router.get("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_get_row(
    request: Request,
    bill_id: int,
    payment_method: str | None = None,
    importance: str | None = None,
    marks: str | None = None,
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    return HTMLResponse(
        _render_bill_row(
            request,
            db,
            bill,
            _list_query(payment_method, importance, marks),
            show_marks=marks == "1",
        )
    )


@router.get("/bills/{bill_id}/edit", response_class=HTMLResponse)
async def bills_edit_form(
    request: Request,
    bill_id: int,
    payment_method: str | None = None,
    importance: str | None = None,
    marks: str | None = None,
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    categories = _expense_categories(db)
    return HTMLResponse(
        _render_edit_row(
            request,
            db,
            bill,
            categories,
            filter_query=_list_query(payment_method, importance, marks),
        )
    )


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
    if origin == "strip":
        return HTMLResponse(_render_strip_confirm(request, bill))
    return HTMLResponse(_render_confirm_fields(request, bill))


@router.post("/bills/{bill_id}/confirm", response_class=HTMLResponse)
async def bills_confirm(
    request: Request,
    bill_id: int,
    origin: str = Query("bills", alias="from"),
    payment_method: str | None = None,
    importance: str | None = None,
    marks: str | None = None,
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    kept_status = 200 if origin in {"dashboard", "card"} else 422
    try:
        amount = Decimal(str(form.get("amount") or "0"))
    except InvalidOperation, TypeError:
        return _occurrence_error(origin, bill_id, "Invalid amount.", kept_status)
    if amount <= 0:
        return _occurrence_error(
            origin, bill_id, "Amount must be greater than zero.", kept_status
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
        if origin in {"dashboard", "card"}:
            return _html_error(exc)
        status = 400 if isinstance(exc, PaymentBlocked) else 409
        return _editor_error(
            exc.detail, status, retarget=_error_target(origin, bill_id)
        )
    except ValueError:
        db.rollback()
        return _occurrence_error(origin, bill_id, "Invalid due date.", kept_status)
    return _after_payment(
        request,
        db,
        result.bill,
        origin,
        result.message,
        payment_method=payment_method,
        importance=importance,
        marks=marks,
    )


@router.post("/bills/{bill_id}/skip", response_class=HTMLResponse)
async def bills_skip(
    request: Request,
    bill_id: int,
    origin: str = Query("bills", alias="from"),
    payment_method: str | None = None,
    importance: str | None = None,
    marks: str | None = None,
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    due_date = str(form.get("due_date") or "").strip()
    kept_status = 200 if origin in {"dashboard", "card"} else 422
    try:
        result = skip_payment(db, bill, due_date)
        db.commit()
        db.refresh(result.bill)
    except UnknownFrequencyError as exc:
        db.rollback()
        return _unknown_frequency_html(exc)
    except (PaymentBlocked, PaymentConflict) as exc:
        db.rollback()
        if origin in {"dashboard", "card"}:
            return _html_error(exc)
        status = 400 if isinstance(exc, PaymentBlocked) else 409
        return _editor_error(
            exc.detail, status, retarget=_error_target(origin, bill_id)
        )
    except ValueError:
        db.rollback()
        return _occurrence_error(origin, bill_id, "Invalid due date.", kept_status)
    return _after_payment(
        request,
        db,
        result.bill,
        origin,
        result.message,
        payment_method=payment_method,
        importance=importance,
        marks=marks,
    )


@router.post("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_update(
    request: Request,
    bill_id: int,
    payment_method: str | None = None,
    importance: str | None = None,
    marks: str | None = None,
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)

    before = (
        Decimal(str(bill.amount)),
        bill.frequency,
        bill.paid_from,
        bill.paid_from_fund_id,
        bill.is_active,
        bill.importance,
    )
    form = await request.form()
    importance_set = "importance" in form
    cancel_set = "cancel_note" in form
    stopped_set = "if_stopped_note" in form
    try:
        parsed_importance = (
            _read_importance(form.get("importance")) if importance_set else None
        )
    except BillFieldError as exc:
        return _editor_error(exc.message, 422, focus=f"#importance-{bill.id}")
    try:
        parsed_cancel = clean_bill_note(form.get("cancel_note")) if cancel_set else None
    except BillFieldError as exc:
        return _editor_error(exc.message, 422, focus=f"#cancel-note-{bill.id}")
    try:
        parsed_stopped = (
            clean_bill_note(form.get("if_stopped_note")) if stopped_set else None
        )
    except BillFieldError as exc:
        return _editor_error(exc.message, 422, focus=f"#stopped-note-{bill.id}")

    next_due_raw = str(form.get("next_due_date") or "").strip()
    was_raw = str(form.get("next_due_date_was") or "").strip()
    try:
        next_due = date.fromisoformat(next_due_raw).isoformat()
        expected_due = date.fromisoformat(was_raw).isoformat()
    except ValueError:
        return _editor_error("Enter a real next due date.", 400)

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
            return _editor_error("Choose a frequency.", 400)
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
            return _editor_error("Payment method not found or archived.", 422)
        try:
            assign_payment_method(db, bill, new_id)
        except PaymentMethodError as exc:
            db.rollback()
            return _editor_error(exc.message, exc.status)

    if "contract_id" in form:
        raw_contract = str(form.get("contract_id") or "").strip()
        try:
            contract_id = int(raw_contract) if raw_contract else None
        except ValueError:
            db.rollback()
            return _editor_error("Contract not found or ended.", 422)
        try:
            assign_contract(db, bill, contract_id)
        except ContractError as exc:
            db.rollback()
            return _editor_error(exc.message, exc.status)

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
            return _editor_error(exc.message, 400)
        except ValueError:
            db.rollback()
            return _editor_error("Choose where this is paid from.", 400)

    if importance_set:
        bill.importance = parsed_importance
    if cancel_set:
        bill.cancel_note = parsed_cancel
    if stopped_set:
        bill.if_stopped_note = parsed_stopped

    values = pending_bill_values(bill)
    values.pop("next_due_date", None)
    values["next_due_date"] = next_due
    try:
        cas_save_bill(db, bill, values, expected_due)
    except PaymentConflict as exc:
        db.rollback()
        fresh = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
        expected = fresh.next_due_date if fresh is not None else ""
        extra = (
            f'<input id="edit-due-was-{bill_id}" hx-swap-oob="outerHTML" '
            f'type="hidden" name="next_due_date_was" value="{expected}">'
        )
        return _editor_error(exc.detail, 409, extra=extra)

    db.commit()
    db.refresh(bill)

    filter_query = _list_query(payment_method, importance, marks)
    show_marks = marks == "1"
    strip = "" if show_marks else _oob(_render_due_soon(request, db), "outerHTML")
    still_listed = bill in _filter_bills([bill], payment_method, importance)
    if (payment_method or importance) and not still_listed:
        response = HTMLResponse(strip + _EMPTY_DRAWER)
        response.headers["HX-Reswap"] = "delete"
    else:
        response = HTMLResponse(
            _render_bill_row(request, db, bill, filter_query, show_marks=show_marks)
            + strip
            + _EMPTY_DRAWER
        )
    after = (
        Decimal(str(bill.amount)),
        bill.frequency,
        bill.paid_from,
        bill.paid_from_fund_id,
        bill.is_active,
        bill.importance,
    )
    if before != after:
        response.headers["HX-Trigger-After-Swap"] = _summary_trigger(
            _bill_context(db), "Saved.", bill.id
        )
    return response


@router.delete("/bills/{bill_id}", response_class=HTMLResponse)
async def bills_delete(
    request: Request,
    bill_id: int,
    marks: str | None = None,
    db: Session = Depends(get_db),
):
    bill = db.query(RecurringBill).filter(RecurringBill.id == bill_id).first()
    if not bill:
        return HTMLResponse("Not found", status_code=404)
    bill.is_active = False
    db.commit()
    strip = "" if marks == "1" else _oob(_render_due_soon(request, db), "outerHTML")
    response = HTMLResponse(strip + _EMPTY_DRAWER)
    response.headers["HX-Trigger-After-Swap"] = _summary_trigger(
        _bill_context(db), "Saved.", bill_id
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
        importance=data.importance.value if data.importance is not None else None,
        cancel_note=data.cancel_note,
        if_stopped_note=data.if_stopped_note,
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
