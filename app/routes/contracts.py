"""Contract pages and JSON API."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware import get_current_user
from app.models import Contract, RecurringBill
from app.schemas import (
    ContractCreate,
    ContractResponse,
    ContractUpdate,
    public_validation_errors,
)
from app.services.contracts import (
    DELIVERY_LABELS,
    SERVICE_LABELS,
    ContractError,
    apply_contract,
    contract_choice_label,
    contract_label,
    delete_contract,
    delivery_value,
    end_contract,
    form_to_payload,
    linked_bills,
    list_contracts,
    merge_contract,
    prepare_contract,
    service_value,
    status_label,
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


def _json_error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=status)


def _validation_json(exc: ValidationError) -> JSONResponse:
    return JSONResponse({"detail": public_validation_errors(exc)}, status_code=422)


def _wants_ended(include_ended: bool, ended: str | None) -> bool:
    return include_ended or ended in ("1", "true", "yes")


def _get_contract(db: Session, contract_id: int) -> Contract | None:
    return db.get(Contract, contract_id)


def _dump(contract: Contract) -> dict:
    return ContractResponse.model_validate(contract).model_dump(mode="json")


def _rows(db: Session, contracts: list[Contract]) -> list[dict]:
    rows = []
    for contract in contracts:
        bills = linked_bills(db, contract.id)
        rows.append(
            {
                "contract": contract,
                "label": contract_label(contract),
                "plan": contract.plan_name or "",
                "service_label": SERVICE_LABELS.get(contract.service, contract.service),
                "delivery_label": DELIVERY_LABELS.get(
                    contract.bill_delivery, contract.bill_delivery
                ),
                "linked_count": len(bills),
                "status": status_label(contract),
            }
        )
    return rows


def _list_context(
    request: Request, db: Session, *, show_ended: bool, error: str | None
):
    user = get_current_user(request)
    contracts = list_contracts(db, include_ended=show_ended)
    return {
        "username": user.username,
        "page": "list",
        "rows": _rows(db, contracts),
        "service_labels": SERVICE_LABELS,
        "delivery_labels": DELIVERY_LABELS,
        "show_ended": show_ended,
        "flash": _pop_flash(request),
        "error": error,
    }


def _render_list(
    request: Request,
    db: Session,
    *,
    show_ended: bool,
    error: str | None,
    status: int,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "contracts.html",
        _list_context(request, db, show_ended=show_ended, error=error),
        status_code=status,
    )


def _detail_context(
    request: Request,
    db: Session,
    contract: Contract,
    *,
    error: str | None,
) -> dict:
    user = get_current_user(request)
    bills = linked_bills(db, contract.id)
    return {
        "username": user.username,
        "page": "detail",
        "contract": contract,
        "label": contract_label(contract),
        "choice_label": contract_choice_label(contract),
        "status": status_label(contract),
        "bills": bills,
        "linked_count": len(bills),
        "service_labels": SERVICE_LABELS,
        "delivery_labels": DELIVERY_LABELS,
        "error": error,
        "flash": _pop_flash(request),
    }


def _render_detail(
    request: Request,
    db: Session,
    contract: Contract,
    *,
    error: str | None,
    status: int,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "contracts.html",
        _detail_context(request, db, contract, error=error),
        status_code=status,
    )


def _patch_from_update(parsed: ContractUpdate) -> dict:
    patch: dict = {}
    fields = parsed.model_fields_set
    if "name" in fields:
        patch["name"] = parsed.name
    if "provider" in fields:
        patch["provider"] = parsed.provider
    if "plan_name" in fields:
        patch["plan_name"] = parsed.plan_name
    if "service" in fields:
        patch["service"] = service_value(parsed.service)
    if "bill_delivery" in fields:
        patch["bill_delivery"] = delivery_value(parsed.bill_delivery)
    if "delivery_detail" in fields:
        patch["delivery_detail"] = parsed.delivery_detail
    if "notes" in fields:
        patch["notes"] = parsed.notes
    if "started_on" in fields:
        patch["started_on"] = parsed.started_on
    if "ended_on" in fields:
        patch["ended_on"] = parsed.ended_on
    return patch


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------


@router.get("/contracts", response_class=HTMLResponse)
async def contracts_page(
    request: Request,
    ended: str | None = None,
    db: Session = Depends(get_db),
):
    return _render_list(
        request,
        db,
        show_ended=_wants_ended(False, ended),
        error=None,
        status=200,
    )


@router.post("/contracts", response_class=HTMLResponse)
async def contracts_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    try:
        values = prepare_contract(form_to_payload(form, None), existing=None)
        contract = Contract(**values)
        db.add(contract)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        return _render_list(
            request, db, show_ended=False, error=_error_text(exc), status=422
        )
    except ContractError as exc:
        db.rollback()
        return _render_list(
            request, db, show_ended=False, error=exc.message, status=exc.status
        )
    return _redirect(request, "/contracts", "Contract added.")


@router.get("/contracts/{contract_id}", response_class=HTMLResponse)
async def contracts_detail(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return HTMLResponse("Not found", status_code=404)
    return _render_detail(request, db, contract, error=None, status=200)


@router.post("/contracts/{contract_id}", response_class=HTMLResponse)
async def contracts_update(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    try:
        values = prepare_contract(form_to_payload(form, contract), existing=contract)
        apply_contract(contract, values)
        db.commit()
    except ValidationError as exc:
        db.rollback()
        contract = _get_contract(db, contract_id)
        if contract is None:
            return HTMLResponse("Not found", status_code=404)
        return _render_detail(request, db, contract, error=_error_text(exc), status=422)
    except ContractError as exc:
        db.rollback()
        contract = _get_contract(db, contract_id)
        if contract is None:
            return HTMLResponse("Not found", status_code=404)
        return _render_detail(
            request, db, contract, error=exc.message, status=exc.status
        )
    return _redirect(request, f"/contracts/{contract_id}", "Saved.")


@router.post("/contracts/{contract_id}/end", response_class=HTMLResponse)
async def contracts_end(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return HTMLResponse("Not found", status_code=404)
    end_contract(contract)
    db.commit()
    return _redirect(
        request,
        f"/contracts/{contract_id}",
        "Ended. Linked bills are unchanged and will still be paid / forecast as usual.",
    )


@router.post("/contracts/{contract_id}/unlink", response_class=HTMLResponse)
async def contracts_unlink(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return HTMLResponse("Not found", status_code=404)
    form = await request.form()
    raw = str(form.get("bill_id") or "").strip()
    try:
        bill_id = int(raw)
    except ValueError:
        return _render_detail(
            request,
            db,
            contract,
            error="That bill is not linked to this contract.",
            status=422,
        )
    bill = db.get(RecurringBill, bill_id)
    if bill is None or bill.contract_id != contract.id:
        return _render_detail(
            request,
            db,
            contract,
            error="That bill is not linked to this contract.",
            status=422,
        )
    bill.contract_id = None
    db.commit()
    return _redirect(request, f"/contracts/{contract_id}", "Unlinked.")


@router.post("/contracts/{contract_id}/delete", response_class=HTMLResponse)
async def contracts_delete(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return HTMLResponse("Not found", status_code=404)
    try:
        delete_contract(db, contract)
        db.commit()
    except ContractError as exc:
        db.rollback()
        contract = _get_contract(db, contract_id)
        if contract is None:
            return HTMLResponse("Not found", status_code=404)
        return _render_detail(
            request, db, contract, error=exc.message, status=exc.status
        )
    return _redirect(request, "/contracts", "Deleted.")


# ---------------------------------------------------------------------------
# JSON API
# ---------------------------------------------------------------------------


@router.get("/api/contracts")
async def api_list_contracts(
    request: Request,
    include_ended: bool = False,
    ended: str | None = None,
    db: Session = Depends(get_db),
):
    rows = list_contracts(db, include_ended=_wants_ended(include_ended, ended))
    return [_dump(row) for row in rows]


@router.post("/api/contracts")
async def api_create_contract(request: Request, db: Session = Depends(get_db)):
    try:
        body = await request.json()
        parsed = ContractCreate.model_validate(body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            return _validation_json(exc)
        return _json_error("Invalid contract.", 422)
    try:
        values = prepare_contract(
            {
                "name": parsed.name,
                "provider": parsed.provider,
                "plan_name": parsed.plan_name,
                "service": parsed.service.value,
                "bill_delivery": parsed.bill_delivery.value,
                "delivery_detail": parsed.delivery_detail,
                "notes": parsed.notes,
                "started_on": parsed.started_on,
                "ended_on": parsed.ended_on,
            },
            existing=None,
        )
    except ContractError as exc:
        return _json_error(exc.message, exc.status)
    contract = Contract(**values)
    db.add(contract)
    db.commit()
    db.refresh(contract)
    return JSONResponse(_dump(contract), status_code=201)


@router.get("/api/contracts/{contract_id}")
async def api_get_contract(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return _json_error("Contract not found.", 404)
    return JSONResponse(_dump(contract))


@router.put("/api/contracts/{contract_id}")
async def api_update_contract(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return _json_error("Contract not found.", 404)
    try:
        body = await request.json()
        parsed = ContractUpdate.model_validate(body)
    except (ValidationError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            return _validation_json(exc)
        return _json_error("Invalid contract.", 422)
    try:
        values = prepare_contract(
            merge_contract(contract, _patch_from_update(parsed)),
            existing=contract,
        )
    except ValidationError as exc:
        return _validation_json(exc)
    except ContractError as exc:
        return _json_error(exc.message, exc.status)
    apply_contract(contract, values)
    db.commit()
    db.refresh(contract)
    return JSONResponse(_dump(contract))


@router.post("/api/contracts/{contract_id}/end")
async def api_end_contract(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return _json_error("Contract not found.", 404)
    end_contract(contract)
    db.commit()
    db.refresh(contract)
    return JSONResponse(_dump(contract))


@router.delete("/api/contracts/{contract_id}")
async def api_delete_contract(
    request: Request, contract_id: int, db: Session = Depends(get_db)
):
    contract = _get_contract(db, contract_id)
    if contract is None:
        return _json_error("Contract not found.", 404)
    try:
        delete_contract(db, contract)
        db.commit()
    except ContractError as exc:
        db.rollback()
        return _json_error(exc.message, exc.status)
    return JSONResponse({"detail": "Deleted."})
