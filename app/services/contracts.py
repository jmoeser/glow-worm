"""Household contracts: plans, delivery, and which bill they belong to.

One rule set for the web UI and the JSON API. A payment stamps the bill's
contract at pay time and that stamp is not rewritten when the bill later
points at a different contract.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.config import TIMEZONE
from app.models import Contract, RecurringBill, Transaction
from app.schemas import BillDelivery, ContractCreate, ContractService

SERVICE_LABELS = {
    "electricity": "Electricity",
    "health_insurance": "Health insurance",
    "internet": "Internet",
    "phone": "Phone",
    "car_insurance": "Car insurance",
    "other": "Other",
}

DELIVERY_LABELS = {
    "email": "Email",
    "post": "Post",
    "app": "App",
    "other": "Other",
}

_FORM_FIELDS = (
    "name",
    "provider",
    "plan_name",
    "service",
    "bill_delivery",
    "delivery_detail",
    "notes",
    "started_on",
    "ended_on",
)


class ContractError(Exception):
    """A rule failure with a message safe to show."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def brisbane_today() -> date:
    return datetime.now(TIMEZONE).date()


def contract_label(contract: Contract) -> str:
    """``name — provider — plan``, dropping an empty plan."""
    parts = [contract.name, contract.provider]
    if contract.plan_name:
        parts.append(contract.plan_name)
    return " — ".join(parts)


def contract_choice_label(contract: Contract) -> str:
    label = contract_label(contract)
    if contract.ended_on:
        return f"{label} (Ended)"
    return label


def status_label(contract: Contract) -> str:
    """``Active`` or ``Ended D Mon YYYY`` from the stored calendar date."""
    if not contract.ended_on:
        return "Active"
    parsed = date.fromisoformat(contract.ended_on)
    return f"Ended {parsed.day} {calendar.month_abbr[parsed.month]} {parsed.year}"


def contract_payload(contract: Contract) -> dict:
    return {
        "name": contract.name,
        "provider": contract.provider,
        "plan_name": contract.plan_name,
        "service": contract.service,
        "bill_delivery": contract.bill_delivery,
        "delivery_detail": contract.delivery_detail,
        "notes": contract.notes,
        "started_on": contract.started_on,
        "ended_on": contract.ended_on,
    }


def form_to_payload(form, existing: Contract | None) -> dict:
    """Read a web form. Absent fields keep the stored value."""
    if existing is None:
        data = dict.fromkeys(_FORM_FIELDS, "")
    else:
        data = contract_payload(existing)
    for key in _FORM_FIELDS:
        if key in form:
            data[key] = str(form.get(key) or "")
    return data


def prepare_contract(data: dict, *, existing: Contract | None) -> dict:
    """Validate a full create payload, or a patch merged onto the stored row.

    Clearing ``ended_on`` is rejected when the row is already ended. A future
    ``ended_on`` is rejected. ``End`` does not use this: it sets today itself.
    """
    parsed = ContractCreate.model_validate(data)
    if (
        existing is not None
        and existing.ended_on is not None
        and parsed.ended_on is None
    ):
        raise ContractError("An ended contract stays ended.")
    if (
        parsed.ended_on is not None
        and date.fromisoformat(parsed.ended_on) > brisbane_today()
    ):
        raise ContractError("End date can't be in the future.")
    return {
        "name": parsed.name,
        "provider": parsed.provider,
        "plan_name": parsed.plan_name,
        "service": parsed.service.value,
        "bill_delivery": parsed.bill_delivery.value,
        "delivery_detail": parsed.delivery_detail,
        "notes": parsed.notes,
        "started_on": parsed.started_on,
        "ended_on": parsed.ended_on,
    }


def apply_contract(contract: Contract, values: dict) -> None:
    contract.name = values["name"]
    contract.provider = values["provider"]
    contract.plan_name = values["plan_name"]
    contract.service = values["service"]
    contract.bill_delivery = values["bill_delivery"]
    contract.delivery_detail = values["delivery_detail"]
    contract.notes = values["notes"]
    contract.started_on = values["started_on"]
    contract.ended_on = values["ended_on"]


def list_contracts(db: Session, *, include_ended: bool) -> list[Contract]:
    query = db.query(Contract)
    if not include_ended:
        query = query.filter(Contract.ended_on.is_(None))
    rows = query.all()
    rows.sort(key=lambda row: (row.name.lower(), row.provider.lower(), row.id))
    return rows


def linked_bills(db: Session, contract_id: int) -> list[RecurringBill]:
    """Every bill row pointing here, active first. Inactive rows still count."""
    return (
        db.query(RecurringBill)
        .filter(RecurringBill.contract_id == contract_id)
        .order_by(RecurringBill.is_active.desc(), RecurringBill.name, RecurringBill.id)
        .all()
    )


def contract_choices(db: Session, current_id: int | None) -> list[Contract]:
    """Active contracts, plus the bill's current contract when that one has ended."""
    chosen = db.query(Contract).filter(Contract.ended_on.is_(None)).all()
    ids = {row.id for row in chosen}
    if current_id is not None and current_id not in ids:
        current = db.get(Contract, current_id)
        if current is not None:
            chosen.append(current)
    chosen.sort(key=lambda row: (row.name.lower(), row.provider.lower(), row.id))
    return chosen


def assign_contract(db: Session, bill: RecurringBill, new_id: int | None) -> None:
    """Set or clear a bill's contract. Re-posting the current id is always allowed."""
    if new_id is not None and new_id == bill.contract_id:
        return
    if new_id is None:
        bill.contract_id = None
        return
    contract = db.get(Contract, new_id)
    if contract is None or contract.ended_on is not None:
        raise ContractError("Contract not found or ended.")
    bill.contract_id = new_id


def end_contract(contract: Contract, today: date | None = None) -> None:
    """Set ``ended_on`` to today in Australia/Brisbane. Bills are left alone."""
    when = today if today is not None else brisbane_today()
    contract.ended_on = when.isoformat()


def delete_contract(db: Session, contract: Contract) -> list[int]:
    """Hard-delete when no bill row still points here.

    Stamped payments are cleared in the app. SQLite does not enforce the
    ``ON DELETE SET NULL`` foreign key, and a bill link is a 409 instead of
    a silent clear.

    Returns the transaction ids whose ``contract_id`` was cleared.
    """
    linked = (
        db.query(RecurringBill.id)
        .filter(RecurringBill.contract_id == contract.id)
        .first()
    )
    if linked is not None:
        raise ContractError(
            "A bill still links to this contract. Unlink it, or end the contract instead.",
            409,
        )
    txn_ids = [
        txn_id
        for (txn_id,) in db.query(Transaction.id)
        .filter(Transaction.contract_id == contract.id)
        .all()
    ]
    (
        db.query(Transaction)
        .filter(Transaction.contract_id == contract.id)
        .update({Transaction.contract_id: None}, synchronize_session=False)
    )
    db.delete(contract)
    return txn_ids


def merge_contract(existing: Contract, patch: dict) -> dict:
    data = contract_payload(existing)
    data.update(patch)
    return data


def service_value(value: ContractService | None) -> str | None:
    return value.value if isinstance(value, ContractService) else None


def delivery_value(value: BillDelivery | None) -> str | None:
    return value.value if isinstance(value, BillDelivery) else None
