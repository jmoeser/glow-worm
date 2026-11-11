"""Payment method pages and JSON API."""

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.audit import (
    audit_write,
    column_changes,
    emit_rest,
    snapshot_columns,
    summary_for,
)
from app.config import TIMEZONE
from app.database import get_db
from app.middleware import get_current_user
from app.models import PaymentMethod, PaymentMethodReplacement
from app.schemas import (
    FREQUENCY_LABELS,
    PaymentMethodCreate,
    PaymentMethodUpdate,
    public_validation_errors,
)
from app.services.payment_methods import (
    KIND_LABELS,
    PROVIDER_LABELS,
    PaymentMethodError,
    _any_linked_bill,
    also_used_by,
    apply_payment_method,
    archive_method,
    bills_word,
    delete_method,
    effective_expiry,
    end_of_phrase,
    expires_label,
    expiry_status,
    finish,
    form_to_payload,
    funded_wallets,
    funding_card_choices,
    linked_items,
    local_date,
    merge_payment_method,
    method_label,
    method_status,
    open_replacements,
    owner_suggestions,
    payment_method_expiry_warn_days,
    prepare_payment_method,
    progress,
    replacement_payload,
    save_note,
    serialize_payment_method,
    short_date,
    start,
    stop,
    tick,
    tick_wallet,
    unarchive_method,
    untick,
    untick_wallet,
)
from app.templating import templates

router = APIRouter()


def _redirect(request: Request, url: str, message: str | None = None):
    if message:
        request.session["flash"] = message
    if request.headers.get("hx-request"):
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = url
        return response
    return RedirectResponse(url, status_code=303)


def _pop_flash(request: Request) -> str | None:
    message = request.session.get("flash")
    if message:
        request.session.pop("flash", None)
    return message


def _error_text(exc: ValidationError) -> str:
    return " ".join(str(err["msg"]) for err in public_validation_errors(exc))


def _edit_error(
    request: Request, db: Session, method_id: int, message: str, status: int
) -> HTMLResponse:
    user = get_current_user(request)
    method = _get_method(db, method_id)
    return templates.TemplateResponse(
        request,
        "payment_methods.html",
        {
            "username": user.username,
            "page": "edit",
            "method": method,
            "month_value": _month_value(method) if method else "",
            "owners": owner_suggestions(db, user.username),
            "kind_labels": KIND_LABELS,
            "provider_labels": PROVIDER_LABELS,
            "funding_cards": funding_card_choices(
                db,
                current_id=method.funding_method_id if method else None,
                editing_id=method.id if method else None,
            ),
            "also_used_by": also_used_by(db, method) if method else None,
            "error": message,
        },
        status_code=status,
    )


def _html_error(message: str, status: int) -> HTMLResponse:
    return HTMLResponse(
        f'<p class="gw-msg-error">{message}</p>',
        status_code=status,
    )


def _json_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _validation_json(exc: ValidationError) -> JSONResponse:
    return JSONResponse({"detail": public_validation_errors(exc)}, status_code=422)


def _get_method(db: Session, method_id: int) -> PaymentMethod | None:
    return db.get(PaymentMethod, method_id)


def _open_for_old(db: Session, old_id: int) -> PaymentMethodReplacement | None:
    return (
        db.query(PaymentMethodReplacement)
        .filter(
            PaymentMethodReplacement.old_id == old_id,
            PaymentMethodReplacement.status == "open",
        )
        .first()
    )


def _month_value(method: PaymentMethod) -> str:
    if method.expiry_year and method.expiry_month:
        return f"{method.expiry_year:04d}-{method.expiry_month:02d}"
    return ""


def _expires_text(method: PaymentMethod) -> str:
    return expires_label(method)


def _start_choices(db: Session, old_id: int) -> list[PaymentMethod]:
    busy: set[int] = set()
    for rep in open_replacements(db):
        busy.add(rep.old_id)
        busy.add(rep.new_id)
    methods = (
        db.query(PaymentMethod)
        .filter(PaymentMethod.is_archived == False, PaymentMethod.id != old_id)
        .all()
    )
    chosen = [method for method in methods if method.id not in busy]
    if funded_wallets(db, old_id):
        chosen = [method for method in chosen if method.kind == "card"]
    chosen.sort(
        key=lambda method: (method.owner.lower(), method.nickname.lower(), method.id)
    )
    return chosen


def _method_rows(db: Session, methods: list[PaymentMethod]) -> list[dict]:
    today = datetime.now(TIMEZONE).date()
    warn_days = payment_method_expiry_warn_days()
    reps = open_replacements(db)
    old_ids = {rep.old_id for rep in reps}
    new_ids = {rep.new_id for rep in reps}
    funding_ids = {
        funding_id
        for (funding_id,) in db.query(PaymentMethod.funding_method_id)
        .filter(PaymentMethod.funding_method_id.is_not(None))
        .all()
        if funding_id is not None
    }
    rows = []
    for method in methods:
        used = linked_items(db, method)
        linked = _any_linked_bill(db, method.id) is not None
        funds = funded_wallets(db, method.id) if method.kind == "card" else []
        wallet_linked = method.id in funding_ids
        open_rep = method.id in old_ids or method.id in new_ids
        blocked: list[str] = []
        replace_href = None
        replace_label = None
        if method.id in old_ids:
            replace_href = f"/payment-methods/{method.id}/replace"
            replace_label = "Continue"
        elif method.id in new_ids:
            blocked.append("Finish or stop replacing first.")
        elif method.is_archived:
            pass
        elif not used and not funds:
            blocked.append("No active bills use this. Archive it instead.")
        else:
            replace_href = f"/payment-methods/{method.id}/replace"
            replace_label = "Replace"
        if open_rep and "Finish or stop replacing first." not in blocked:
            blocked.append("Finish or stop replacing first.")
        elif used and not method.is_archived and not open_rep:
            names = ", ".join(item.name for item in used)
            blocked.append(f"Move these bills first: {names}.")
        if linked and not open_rep and not used:
            blocked.append("A bill still links to this.")
        if wallet_linked:
            blocked.append("A wallet still links to this.")
        rows.append(
            {
                "method": method,
                "label": method_label(method),
                "kind_label": KIND_LABELS.get(method.kind, method.kind),
                "expires": _expires_text(method),
                "used_label": f"{len(used)} {bills_word(len(used))}",
                "also_used_by": also_used_by(db, method),
                "status": method_status(
                    method,
                    is_old=method.id in old_ids,
                    is_new=method.id in new_ids,
                    today=today,
                    warn_days=warn_days,
                ),
                "replace_href": replace_href,
                "replace_label": replace_label,
                "archive_ok": not open_rep and not used and not method.is_archived,
                "delete_ok": not open_rep and not linked and not wallet_linked,
                "blocked": blocked,
            }
        )
    return rows


def _boxes(db: Session) -> list[dict]:
    boxes = []
    for rep in open_replacements(db):
        old = db.get(PaymentMethod, rep.old_id)
        new = db.get(PaymentMethod, rep.new_id)
        if old is None or new is None:
            continue
        prog = progress(db, rep)
        phrase = prog.phrase
        if prog.due_soon_count:
            phrase = f"{phrase}, {prog.due_soon_count} due soon"
        boxes.append(
            {
                "old_label": method_label(old),
                "new_label": method_label(new),
                "phrase": phrase,
                "started": short_date(local_date(rep.started_at)),
                "href": f"/payment-methods/{old.id}/replace",
            }
        )
    return boxes


def _list_context(
    request: Request, db: Session, *, show_archived: bool, error: str | None
):
    user = get_current_user(request)
    query = db.query(PaymentMethod)
    if not show_archived:
        query = query.filter(PaymentMethod.is_archived == False)
    methods = query.all()
    methods.sort(
        key=lambda method: (method.owner.lower(), method.nickname.lower(), method.id)
    )
    context = {
        "username": user.username,
        "page": "list",
        "rows": _method_rows(db, methods),
        "boxes": _boxes(db),
        "owners": owner_suggestions(db, user.username),
        "kind_labels": KIND_LABELS,
        "provider_labels": PROVIDER_LABELS,
        "funding_cards": funding_card_choices(db),
        "show_archived": show_archived,
        "flash": _pop_flash(request),
        "error": error,
    }
    db.commit()
    return context


def _render_list(
    request: Request,
    db: Session,
    *,
    show_archived: bool,
    error: str | None,
    status: int,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "payment_methods.html",
        _list_context(request, db, show_archived=show_archived, error=error),
        status_code=status,
    )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@router.get("/payment-methods", response_class=HTMLResponse)
async def payment_methods_page(
    request: Request,
    archived: str | None = None,
    db: Session = Depends(get_db),
):
    show = archived in ("1", "true", "yes")
    return _render_list(request, db, show_archived=show, error=None, status=200)


@router.post("/payment-methods", response_class=HTMLResponse)
async def payment_methods_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        payload = form_to_payload(form, None)
        values = prepare_payment_method(
            payload.data,
            explicit_last4=payload.explicit_last4,
            explicit_expiry=payload.explicit_expiry,
            explicit_provider=payload.explicit_provider,
            explicit_funding=payload.explicit_funding,
            db=db,
        )
        method = PaymentMethod(**values)
        db.add(method)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_list(
            request, db, show_archived=False, error=_error_text(exc), status=422
        )
    except PaymentMethodError as exc:
        db.rollback()
        return _render_list(
            request, db, show_archived=False, error=exc.message, status=exc.status
        )
    return _redirect(request, "/payment-methods", "Payment method added.")


@router.get("/payment-methods/{method_id}/edit", response_class=HTMLResponse)
async def payment_methods_edit_page(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return HTMLResponse("Not found", status_code=404)
    user = get_current_user(request)
    return templates.TemplateResponse(
        request,
        "payment_methods.html",
        {
            "username": user.username,
            "page": "edit",
            "method": method,
            "month_value": _month_value(method),
            "owners": owner_suggestions(db, user.username),
            "kind_labels": KIND_LABELS,
            "provider_labels": PROVIDER_LABELS,
            "funding_cards": funding_card_choices(
                db, current_id=method.funding_method_id, editing_id=method.id
            ),
            "also_used_by": also_used_by(db, method),
            "error": None,
        },
    )


@router.post("/payment-methods/{method_id}", response_class=HTMLResponse)
async def payment_methods_update(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    try:
        payload = form_to_payload(form, method)
        values = prepare_payment_method(
            payload.data,
            explicit_last4=payload.explicit_last4,
            explicit_expiry=payload.explicit_expiry,
            explicit_provider=payload.explicit_provider,
            explicit_funding=payload.explicit_funding,
            db=db,
            existing=method,
        )
        apply_payment_method(method, values)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _edit_error(request, db, method_id, _error_text(exc), 422)
    except PaymentMethodError as exc:
        db.rollback()
        return _edit_error(request, db, method_id, exc.message, exc.status)
    return _redirect(request, "/payment-methods", "Saved.")


@router.post("/payment-methods/{method_id}/archive", response_class=HTMLResponse)
async def payment_methods_archive(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return HTMLResponse("Not found", status_code=404)
    try:
        archive_method(db, method)
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _render_list(
            request, db, show_archived=False, error=exc.message, status=exc.status
        )
    return _redirect(request, "/payment-methods", "Archived.")


@router.post("/payment-methods/{method_id}/unarchive", response_class=HTMLResponse)
async def payment_methods_unarchive(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return HTMLResponse("Not found", status_code=404)
    unarchive_method(method)
    db.commit()
    return _redirect(request, "/payment-methods?archived=1", "Unarchived.")


@router.post("/payment-methods/{method_id}/delete", response_class=HTMLResponse)
async def payment_methods_delete(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return HTMLResponse("Not found", status_code=404)
    try:
        delete_method(db, method)
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _render_list(
            request, db, show_archived=True, error=exc.message, status=exc.status
        )
    return _redirect(request, "/payment-methods", "Deleted.")


def _replace_template(
    request: Request,
    db: Session,
    old: PaymentMethod,
    *,
    page: str,
    error: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    user = get_current_user(request)
    rep = _open_for_old(db, old.id)
    context: dict = {
        "username": user.username,
        "page": page,
        "old": old,
        "old_label": method_label(old),
        "error": error,
        "kind_labels": KIND_LABELS,
        "provider_labels": PROVIDER_LABELS,
        "funding_cards": funding_card_choices(db),
        "owners": owner_suggestions(db, user.username),
        "choices": _start_choices(db, old.id),
        "needs_card": bool(funded_wallets(db, old.id)),
    }
    if rep is not None and page == "replace":
        new = db.get(PaymentMethod, rep.new_id)
        prog = progress(db, rep)
        today = datetime.now(TIMEZONE).date()
        warn_days = payment_method_expiry_warn_days()
        context.update(
            {
                "new": new,
                "new_label": method_label(new) if new is not None else "",
                "prog": prog,
                "expired_warning": expiry_status(old, today, warn_days) == "expired",
                "expired_when": (
                    end_of_phrase(pair[0], pair[1])
                    if (pair := effective_expiry(old)) is not None
                    else ""
                ),
                "frequency_labels": FREQUENCY_LABELS,
            }
        )
        db.commit()
    return templates.TemplateResponse(
        request, "payment_methods.html", context, status_code=status
    )


@router.get("/payment-methods/{method_id}/replace", response_class=HTMLResponse)
async def payment_methods_replace_page(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    page = "replace" if _open_for_old(db, old.id) else "start"
    return _replace_template(request, db, old, page=page)


@router.post("/payment-methods/{method_id}/replace", response_class=HTMLResponse)
async def payment_methods_replace_start(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    existing = str(form.get("existing_id") or "").strip()
    try:
        if existing:
            start(db, old.id, new_id=int(existing))
        else:
            payload = form_to_payload(form, None)
            data = payload.data
            if "nickname" not in form:
                data["nickname"] = old.nickname
                data["owner"] = old.owner
                data["kind"] = "card" if old.kind == "wallet" else old.kind
            start(
                db,
                old.id,
                new_method=data,
                explicit_last4=payload.explicit_last4,
                explicit_expiry=payload.explicit_expiry,
            )
        db.commit()
    except (ValidationError, PaymentMethodError, ValueError) as exc:
        db.rollback()
        if isinstance(exc, ValidationError):
            message, status = _error_text(exc), 422
        elif isinstance(exc, PaymentMethodError):
            message, status = exc.message, exc.status
        else:
            message, status = "Pick a payment method or add a new one.", 422
        return _replace_template(
            request, db, old, page="start", error=message, status=status
        )
    return _redirect(request, f"/payment-methods/{old.id}/replace")


def _bill_id_from_form(form) -> int:
    return int(str(form.get("bill_id") or "0"))


def _wallet_id_from_form(form) -> int:
    return int(str(form.get("wallet_method_id") or "0"))


@router.post("/payment-methods/{method_id}/replace/tick", response_class=HTMLResponse)
async def payment_methods_tick(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    rep = _open_for_old(db, old.id)
    if rep is None:
        return _html_error("This replacement is not open.", 409)
    form = await request.form()
    try:
        tick(db, rep, _bill_id_from_form(form))
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _html_error(exc.message, exc.status)
    return _redirect(request, f"/payment-methods/{old.id}/replace")


@router.post("/payment-methods/{method_id}/replace/untick", response_class=HTMLResponse)
async def payment_methods_untick(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    rep = _open_for_old(db, old.id)
    if rep is None:
        return _html_error("This replacement is not open.", 409)
    form = await request.form()
    try:
        untick(db, rep, _bill_id_from_form(form))
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _html_error(exc.message, exc.status)
    return _redirect(request, f"/payment-methods/{old.id}/replace")


@router.post(
    "/payment-methods/{method_id}/replace/wallet-tick", response_class=HTMLResponse
)
async def payment_methods_wallet_tick(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    rep = _open_for_old(db, old.id)
    if rep is None:
        return _html_error("This replacement is not open.", 409)
    form = await request.form()
    try:
        tick_wallet(db, rep, _wallet_id_from_form(form))
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _html_error(exc.message, exc.status)
    return _redirect(request, f"/payment-methods/{old.id}/replace")


@router.post(
    "/payment-methods/{method_id}/replace/wallet-untick", response_class=HTMLResponse
)
async def payment_methods_wallet_untick(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    rep = _open_for_old(db, old.id)
    if rep is None:
        return _html_error("This replacement is not open.", 409)
    form = await request.form()
    try:
        untick_wallet(db, rep, _wallet_id_from_form(form))
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _html_error(exc.message, exc.status)
    return _redirect(request, f"/payment-methods/{old.id}/replace")


@router.post("/payment-methods/{method_id}/replace/note", response_class=HTMLResponse)
async def payment_methods_note(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    rep = _open_for_old(db, old.id)
    if rep is None:
        return _html_error("This replacement is not open.", 409)
    form = await request.form()
    updated = str(form.get("updated_on") or "").strip() or None
    try:
        save_note(
            db,
            rep,
            _bill_id_from_form(form),
            note=str(form.get("note") or ""),
            updated_on=updated,
        )
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _html_error(_error_text(exc), 422)
    except PaymentMethodError as exc:
        db.rollback()
        return _html_error(exc.message, exc.status)
    return _redirect(request, f"/payment-methods/{old.id}/replace")


@router.get("/payment-methods/{method_id}/replace/finish", response_class=HTMLResponse)
async def payment_methods_finish_page(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    if _open_for_old(db, old.id) is None:
        return _html_error("This replacement is not open.", 409)
    return _replace_template(request, db, old, page="finish")


@router.post("/payment-methods/{method_id}/replace/finish", response_class=HTMLResponse)
async def payment_methods_finish(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    rep = _open_for_old(db, old.id)
    if rep is None:
        return _html_error("This replacement is not open.", 409)
    label = method_label(old)
    try:
        finish(db, rep)
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _replace_template(
            request, db, old, page="finish", error=exc.message, status=exc.status
        )
    return _redirect(request, "/payment-methods", f"Done. {label} is archived.")


@router.get("/payment-methods/{method_id}/replace/stop", response_class=HTMLResponse)
async def payment_methods_stop_page(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    if _open_for_old(db, old.id) is None:
        return _html_error("This replacement is not open.", 409)
    return _replace_template(request, db, old, page="stop")


@router.post("/payment-methods/{method_id}/replace/stop", response_class=HTMLResponse)
async def payment_methods_stop(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    old = _get_method(db, method_id)
    if old is None:
        return HTMLResponse("Not found", status_code=404)
    rep = _open_for_old(db, old.id)
    if rep is None:
        return _html_error("This replacement is not open.", 409)
    try:
        stop(db, rep)
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _html_error(exc.message, exc.status)
    return _redirect(
        request,
        "/payment-methods",
        "Stopped. Bills stay where you left them, and ticked Apple Pay stays on the new card.",
    )


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


def _dump_method(method: PaymentMethod) -> dict:
    return serialize_payment_method(method)


@router.get("/api/payment-methods")
async def api_list_payment_methods(
    request: Request,
    include_archived: bool = False,
    db: Session = Depends(get_db),
):
    query = db.query(PaymentMethod)
    if not include_archived:
        query = query.filter(PaymentMethod.is_archived == False)
    methods = query.order_by(PaymentMethod.owner, PaymentMethod.nickname).all()
    return [_dump_method(method) for method in methods]


@router.post("/api/payment-methods")
@audit_write
async def api_create_payment_method(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        parsed = PaymentMethodCreate.model_validate(body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            return _validation_json(exc)
        return _json_error("Invalid payment method.", 422)
    explicit_last4 = body.get("last4") not in (None, "")
    explicit_expiry = (
        body.get("expiry_month") is not None or body.get("expiry_year") is not None
    )
    explicit_provider = body.get("wallet_provider") not in (None, "")
    explicit_funding = body.get("funding_method_id") is not None
    try:
        values = prepare_payment_method(
            {
                "nickname": parsed.nickname,
                "kind": parsed.kind.value,
                "last4": parsed.last4,
                "expiry_month": parsed.expiry_month,
                "expiry_year": parsed.expiry_year,
                "owner": parsed.owner,
                "wallet_provider": (
                    parsed.wallet_provider.value if parsed.wallet_provider else None
                ),
                "funding_method_id": parsed.funding_method_id,
            },
            explicit_last4=explicit_last4,
            explicit_expiry=explicit_expiry,
            explicit_provider=explicit_provider,
            explicit_funding=explicit_funding,
            db=db,
        )
    except PaymentMethodError as exc:
        return _json_error(exc.message, exc.status)
    method = PaymentMethod(**values)
    db.add(method)
    db.commit()
    db.refresh(method)
    emit_rest(
        request,
        action="create",
        entity_type="payment_method",
        entity_id=method.id,
        values=summary_for("payment_method", snapshot_columns(method)),
    )
    return JSONResponse(_dump_method(method), status_code=201)


@router.get("/api/payment-methods/{method_id}")
async def api_get_payment_method(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return _json_error("Payment method not found.", 404)
    data = _dump_method(method)
    data["replacement"] = replacement_payload(db, method)
    db.commit()
    return JSONResponse(data)


@router.put("/api/payment-methods/{method_id}")
@audit_write
async def api_update_payment_method(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return _json_error("Payment method not found.", 404)
    try:
        body = await request.json()
        parsed = PaymentMethodUpdate.model_validate(body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            return _validation_json(exc)
        return _json_error("Invalid payment method.", 422)
    patch: dict = {}
    fields = parsed.model_fields_set
    if "nickname" in fields:
        patch["nickname"] = parsed.nickname
    if "kind" in fields and parsed.kind is not None:
        patch["kind"] = parsed.kind.value
    if "owner" in fields:
        patch["owner"] = parsed.owner
    if "last4" in fields:
        patch["last4"] = parsed.last4
    if "expiry_month" in fields:
        patch["expiry_month"] = parsed.expiry_month
    if "expiry_year" in fields:
        patch["expiry_year"] = parsed.expiry_year
    if "wallet_provider" in fields:
        patch["wallet_provider"] = (
            parsed.wallet_provider.value if parsed.wallet_provider else None
        )
    if "funding_method_id" in fields:
        patch["funding_method_id"] = parsed.funding_method_id
    explicit_last4 = "last4" in fields and parsed.last4 is not None
    explicit_expiry = (
        "expiry_month" in fields and parsed.expiry_month is not None
    ) or ("expiry_year" in fields and parsed.expiry_year is not None)
    explicit_provider = (
        "wallet_provider" in fields and parsed.wallet_provider is not None
    )
    explicit_funding = (
        "funding_method_id" in fields and parsed.funding_method_id is not None
    )
    try:
        values = prepare_payment_method(
            merge_payment_method(method, patch),
            explicit_last4=explicit_last4,
            explicit_expiry=explicit_expiry,
            explicit_provider=explicit_provider,
            explicit_funding=explicit_funding,
            db=db,
            existing=method,
        )
    except ValidationError as exc:
        return _validation_json(exc)
    except PaymentMethodError as exc:
        return _json_error(exc.message, exc.status)
    before = snapshot_columns(method)
    apply_payment_method(method, values)
    db.commit()
    db.refresh(method)
    emit_rest(
        request,
        action="update",
        entity_type="payment_method",
        entity_id=method.id,
        changes=column_changes(before, snapshot_columns(method)),
    )
    return JSONResponse(_dump_method(method))


@router.delete("/api/payment-methods/{method_id}")
@audit_write
async def api_delete_payment_method(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return _json_error("Payment method not found.", 404)
    values = summary_for("payment_method", snapshot_columns(method))
    try:
        delete_method(db, method)
        db.commit()
    except PaymentMethodError as exc:
        db.rollback()
        return _json_error(exc.message, exc.status)
    emit_rest(
        request,
        action="delete",
        entity_type="payment_method",
        entity_id=method_id,
        values=values,
    )
    return JSONResponse({"detail": "Deleted."})


@router.post("/api/payment-methods/{method_id}/archive")
@audit_write
async def api_archive_payment_method(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return _json_error("Payment method not found.", 404)
    before = snapshot_columns(method)
    try:
        archive_method(db, method)
        db.commit()
        db.refresh(method)
    except PaymentMethodError as exc:
        db.rollback()
        return _json_error(exc.message, exc.status)
    emit_rest(
        request,
        action="archive",
        entity_type="payment_method",
        entity_id=method.id,
        changes=column_changes(before, snapshot_columns(method)),
    )
    return JSONResponse(_dump_method(method))


@router.post("/api/payment-methods/{method_id}/unarchive")
@audit_write
async def api_unarchive_payment_method(
    request: Request, method_id: int, db: Session = Depends(get_db)
):
    method = _get_method(db, method_id)
    if method is None:
        return _json_error("Payment method not found.", 404)
    before = snapshot_columns(method)
    unarchive_method(method)
    db.commit()
    db.refresh(method)
    emit_rest(
        request,
        action="unarchive",
        entity_type="payment_method",
        entity_id=method.id,
        changes=column_changes(before, snapshot_columns(method)),
    )
    return JSONResponse(_dump_method(method))
