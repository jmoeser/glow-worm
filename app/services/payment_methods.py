"""Payment methods, expiry alerts, and saved card-replacement checklists.

One rule set for the web UI, the JSON API, and the MCP tools. A replacement
is a saved job: it changes only when someone ticks, finishes, or stops it.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import TIMEZONE, payment_method_expiry_warn_days
from app.models import (
    PaymentMethod,
    PaymentMethodReplacement,
    PaymentMethodReplacementItem,
    RecurringBill,
    _utcnow,
)
from app.schemas import (
    PaymentMethodCreate,
    PaymentMethodKind,
    ReplacementItemResponse,
    ReplacementNoteInput,
    ReplacementProgressResponse,
)

REPLACEMENT_DUE_SOON_DAYS = 7
LINE_CAP = 5
NAME_CAP = 3

KIND_LABELS = {
    "card": "Card",
    "bank": "Bank account",
    "manual": "BPAY / pay myself",
}


class PaymentMethodError(Exception):
    """A rule failure with a message safe to show. The message has no card number."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def method_label(method: PaymentMethod) -> str:
    if method.last4:
        return f"{method.nickname} ending {method.last4}"
    return method.nickname


def expires_on(month: int, year: int) -> date:
    """Last calendar day the card is still valid."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last)


def expiry_status(method: PaymentMethod, today: date, warn_days: int) -> str | None:
    """``expired``, ``expiring``, or None. Cards only."""
    if method.kind != "card":
        return None
    if method.expiry_month is None or method.expiry_year is None:
        return None
    end = expires_on(method.expiry_month, method.expiry_year)
    if today > end:
        return "expired"
    if end <= today + timedelta(days=warn_days):
        return "expiring"
    return None


def end_of_phrase(month: int, year: int) -> str:
    return f"end of {calendar.month_name[month]} {year}"


def short_date(value: date) -> str:
    return f"{value.day} {calendar.month_abbr[value.month]} {value.year}"


def local_date(value: datetime) -> date:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(TIMEZONE).date()


def _today(today: date | None) -> date:
    return today if today is not None else datetime.now(TIMEZONE).date()


def _warn(warn_days: int | None) -> int:
    return payment_method_expiry_warn_days() if warn_days is None else warn_days


def bills_word(count: int) -> str:
    return "bill" if count == 1 else "bills"


def progress_phrase(done: int, counted: int) -> str:
    return f"{done} of {counted} {bills_word(counted)} moved"


def _date_key(iso: str, bill_id: int) -> tuple[int, str, int]:
    try:
        date.fromisoformat(iso)
    except ValueError:
        return (1, "", bill_id)
    return (0, iso, bill_id)


def _join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    shown = names[:NAME_CAP]
    if len(names) == 3:
        return f"{shown[0]}, {shown[1]} and {shown[2]}"
    extra = len(names) - NAME_CAP
    return f"{shown[0]}, {shown[1]}, {shown[2]} and {extra} more"


def _sorted_bills(bills: list[RecurringBill]) -> list[RecurringBill]:
    return sorted(bills, key=lambda bill: _date_key(bill.next_due_date, bill.id))


@dataclass(frozen=True)
class LinkedItem:
    id: int
    name: str
    provider: str
    amount: Decimal
    frequency: str
    next_due_date: str


def linked_items(db: Session, method: PaymentMethod) -> list[LinkedItem]:
    """In-use bills linked to this method. ``end_date`` is ignored."""
    bills = (
        db.query(RecurringBill)
        .filter(
            RecurringBill.payment_method_id == method.id,
            RecurringBill.is_active == True,
        )
        .order_by(RecurringBill.next_due_date, RecurringBill.id)
        .all()
    )
    return [
        LinkedItem(
            id=bill.id,
            name=bill.name,
            provider=bill.debtor_provider,
            amount=Decimal(str(bill.amount)),
            frequency=bill.frequency,
            next_due_date=bill.next_due_date,
        )
        for bill in bills
    ]


def prepare_payment_method(
    data: dict,
    *,
    explicit_last4: bool,
    explicit_expiry: bool,
) -> dict:
    """Validate a create payload, or a patch merged onto the stored row.

    An explicit non-null expiry on a non-card, or last 4 digits on a manual
    method, is rejected. Other non-card results store a null expiry, and a
    manual result stores a null last 4.
    """
    parsed = PaymentMethodCreate.model_validate(data)
    kind = parsed.kind.value
    last4 = parsed.last4
    month = parsed.expiry_month
    year = parsed.expiry_year
    if kind != PaymentMethodKind.card.value:
        if explicit_expiry and (month is not None or year is not None):
            raise PaymentMethodError("Only a card has an expiry.")
        month = None
        year = None
    if kind == PaymentMethodKind.manual.value:
        if explicit_last4 and last4 is not None:
            raise PaymentMethodError("This type doesn't use last 4 digits.")
        last4 = None
    if kind == PaymentMethodKind.card.value and (
        last4 is None or month is None or year is None
    ):
        raise PaymentMethodError("A card needs the last 4 digits and an expiry.")
    return {
        "nickname": parsed.nickname,
        "kind": kind,
        "last4": last4,
        "expiry_month": month,
        "expiry_year": year,
        "owner": parsed.owner,
    }


def merge_payment_method(existing: PaymentMethod, patch: dict) -> dict:
    data = {
        "nickname": existing.nickname,
        "kind": existing.kind,
        "last4": existing.last4,
        "expiry_month": existing.expiry_month,
        "expiry_year": existing.expiry_year,
        "owner": existing.owner,
    }
    data.update(patch)
    return data


def apply_payment_method(method: PaymentMethod, values: dict) -> None:
    method.nickname = values["nickname"]
    method.kind = values["kind"]
    method.last4 = values["last4"]
    method.expiry_month = values["expiry_month"]
    method.expiry_year = values["expiry_year"]
    method.owner = values["owner"]


def parse_month_input(raw: str) -> tuple[int, int]:
    """Parse ``YYYY-MM`` from ``<input type="month">``. Returns ``(month, year)``."""
    parts = raw.split("-")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise PaymentMethodError("Enter the expiry as a month and year.")
    year = int(parts[0])
    month = int(parts[1])
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise PaymentMethodError("Enter the expiry as a month and year.")
    return month, year


def form_to_payload(form, existing: PaymentMethod | None) -> tuple[dict, bool, bool]:
    """Read a web form. Disabled fields are absent and keep the stored value."""
    data = {
        "nickname": existing.nickname if existing is not None else "",
        "kind": existing.kind if existing is not None else "",
        "owner": existing.owner if existing is not None else "",
        "last4": existing.last4 if existing is not None else None,
        "expiry_month": existing.expiry_month if existing is not None else None,
        "expiry_year": existing.expiry_year if existing is not None else None,
    }
    if "nickname" in form:
        data["nickname"] = str(form.get("nickname") or "")
    if "owner" in form:
        data["owner"] = str(form.get("owner") or "")
    if "kind" in form:
        data["kind"] = str(form.get("kind") or "")
    explicit_last4 = False
    explicit_expiry = False
    if "last4" in form:
        raw = str(form.get("last4") or "").strip()
        if raw == "":
            data["last4"] = None
        else:
            data["last4"] = raw
            explicit_last4 = True
    elif existing is None:
        data["last4"] = None
    if "expiry" in form:
        raw = str(form.get("expiry") or "").strip()
        if raw == "":
            data["expiry_month"] = None
            data["expiry_year"] = None
        else:
            month, year = parse_month_input(raw)
            data["expiry_month"] = month
            data["expiry_year"] = year
            explicit_expiry = True
    elif existing is None:
        data["expiry_month"] = None
        data["expiry_year"] = None
    return data, explicit_last4, explicit_expiry


def _in_use_bills(db: Session, method_id: int) -> list[RecurringBill]:
    return (
        db.query(RecurringBill)
        .filter(
            RecurringBill.payment_method_id == method_id,
            RecurringBill.is_active == True,
        )
        .order_by(RecurringBill.name)
        .all()
    )


def _any_linked_bill(db: Session, method_id: int) -> RecurringBill | None:
    return (
        db.query(RecurringBill)
        .filter(RecurringBill.payment_method_id == method_id)
        .first()
    )


def open_replacements(db: Session) -> list[PaymentMethodReplacement]:
    return (
        db.query(PaymentMethodReplacement)
        .filter(PaymentMethodReplacement.status == "open")
        .all()
    )


def open_old_ids(db: Session) -> set[int]:
    return {row.old_id for row in open_replacements(db)}


def replacement_payload(db: Session, method: PaymentMethod) -> dict | None:
    """Read-only progress for an open replacement that uses this method."""
    rep = open_replacement_for(db, method.id)
    if rep is None:
        return None
    prog = progress(db, rep)
    other_id = rep.new_id if rep.old_id == method.id else rep.old_id
    other = db.get(PaymentMethod, other_id)
    payload = ReplacementProgressResponse(
        old_id=rep.old_id,
        new_id=rep.new_id,
        other_id=other_id,
        other_label=method_label(other) if other is not None else "",
        started_at=rep.started_at,
        done=prog.done,
        counted=prog.counted,
        items=[
            ReplacementItemResponse(
                bill_id=item.bill_id,
                name=item.name,
                done=item.done,
                due_soon=item.due_soon,
                updated_on=item.updated_on,
                note=item.note,
            )
            for item in prog.items
        ],
    )
    return payload.model_dump(mode="json")


def open_replacement_for(
    db: Session, method_id: int
) -> PaymentMethodReplacement | None:
    return (
        db.query(PaymentMethodReplacement)
        .filter(
            PaymentMethodReplacement.status == "open",
            or_(
                PaymentMethodReplacement.old_id == method_id,
                PaymentMethodReplacement.new_id == method_id,
            ),
        )
        .first()
    )


def assign_payment_method(db: Session, bill: RecurringBill, new_id: int | None) -> None:
    """Set or clear a bill's method. Re-posting the current id is always allowed."""
    if new_id is not None and new_id == bill.payment_method_id:
        return
    if new_id is None:
        bill.payment_method_id = None
        return
    method = db.get(PaymentMethod, new_id)
    if method is None or method.is_archived:
        raise PaymentMethodError("Payment method not found or archived.")
    outgoing = (
        db.query(PaymentMethodReplacement)
        .filter(
            PaymentMethodReplacement.status == "open",
            PaymentMethodReplacement.old_id == new_id,
        )
        .first()
    )
    if outgoing is not None:
        raise PaymentMethodError(
            "That payment method is being replaced. Pick the new one."
        )
    bill.payment_method_id = new_id


def archive_method(db: Session, method: PaymentMethod) -> None:
    if open_replacement_for(db, method.id) is not None:
        raise PaymentMethodError("Finish or stop replacing first.", 409)
    used = _in_use_bills(db, method.id)
    if used:
        names = ", ".join(bill.name for bill in used)
        raise PaymentMethodError(f"Move these bills first: {names}.", 409)
    method.is_archived = True


def unarchive_method(method: PaymentMethod) -> None:
    method.is_archived = False


def delete_method(db: Session, method: PaymentMethod) -> None:
    if open_replacement_for(db, method.id) is not None:
        raise PaymentMethodError("Finish or stop replacing first.", 409)
    if _any_linked_bill(db, method.id) is not None:
        raise PaymentMethodError("A bill still links to this.", 409)
    reps = (
        db.query(PaymentMethodReplacement)
        .filter(
            or_(
                PaymentMethodReplacement.old_id == method.id,
                PaymentMethodReplacement.new_id == method.id,
            )
        )
        .all()
    )
    for rep in reps:
        for item in list(rep.items):
            db.delete(item)
        db.delete(rep)
    db.delete(method)


def payment_method_choices(db: Session, current_id: int | None) -> list[PaymentMethod]:
    """Picker rows: not archived, not the old side of an open replacement.

    The bill's current method is included even when it would otherwise be hidden.
    """
    outgoing = open_old_ids(db)
    methods = db.query(PaymentMethod).filter(PaymentMethod.is_archived == False).all()
    chosen = [method for method in methods if method.id not in outgoing]
    ids = {method.id for method in chosen}
    if current_id is not None and current_id not in ids:
        current = db.get(PaymentMethod, current_id)
        if current is not None:
            chosen.append(current)
    chosen.sort(
        key=lambda method: (method.owner.lower(), method.nickname.lower(), method.id)
    )
    return chosen


def owner_suggestions(db: Session, username: str) -> list[str]:
    found = {
        owner for (owner,) in db.query(PaymentMethod.owner).distinct().all() if owner
    }
    trimmed = username.strip()[:60]
    if trimmed:
        found.add(trimmed)
    return sorted(found, key=str.lower)


def method_status(
    method: PaymentMethod,
    *,
    is_old: bool,
    is_new: bool,
    today: date,
    warn_days: int,
) -> str:
    """One badge: Archived, Being replaced, Expired, Expires soon, New card, OK."""
    if method.is_archived:
        return "Archived"
    if is_old:
        return "Being replaced"
    status = expiry_status(method, today, warn_days)
    if status == "expired":
        return "Expired"
    if status == "expiring":
        return "Expires soon"
    if is_new:
        return "New card"
    return "OK"


def bill_badge(
    method: PaymentMethod | None,
    *,
    is_outgoing: bool,
    today: date,
    warn_days: int,
) -> str | None:
    if method is None:
        return None
    if is_outgoing:
        return "To move"
    status = expiry_status(method, today, warn_days)
    if status == "expired":
        return "Expired"
    if status == "expiring":
        return "Expires soon"
    return None


def paid_with_for(
    db: Session, bills: list[RecurringBill], *, today: date | None = None
) -> dict[int, dict[str, str | None]]:
    """Label, badge, and sort text for each bill, keyed by bill id."""
    today = _today(today)
    warn_days = payment_method_expiry_warn_days()
    methods = {method.id: method for method in db.query(PaymentMethod).all()}
    outgoing = open_old_ids(db)
    result: dict[int, dict[str, str | None]] = {}
    for bill in bills:
        method = methods.get(bill.payment_method_id) if bill.payment_method_id else None
        if method is None:
            result[bill.id] = {"label": "Not set", "badge": None, "sort": "Not set"}
            continue
        label = method_label(method)
        result[bill.id] = {
            "label": label,
            "badge": bill_badge(
                method,
                is_outgoing=method.id in outgoing,
                today=today,
                warn_days=warn_days,
            ),
            "sort": label,
        }
    return result


def _add_replacement_item(db: Session, replacement_id: int, bill_id: int) -> None:
    """Insert a checklist row. A duplicate from a concurrent load is skipped."""
    item = PaymentMethodReplacementItem(
        replacement_id=replacement_id,
        recurring_bill_id=bill_id,
    )
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError:
        return


def sync_replacement_items(db: Session, replacement: PaymentMethodReplacement) -> None:
    """Add a row for every in-use bill on the old method that doesn't have one.

    A duplicate insert (two loads at once) is skipped.
    """
    if replacement.status != "open":
        return
    bills = (
        db.query(RecurringBill)
        .filter(
            RecurringBill.is_active == True,
            RecurringBill.payment_method_id == replacement.old_id,
        )
        .all()
    )
    existing = {
        bill_id
        for (bill_id,) in db.query(PaymentMethodReplacementItem.recurring_bill_id)
        .filter(PaymentMethodReplacementItem.replacement_id == replacement.id)
        .all()
    }
    for bill in bills:
        if bill.id in existing:
            continue
        _add_replacement_item(db, replacement.id, bill.id)
        existing.add(bill.id)


@dataclass(frozen=True)
class ItemProgress:
    bill_id: int
    name: str
    provider: str
    amount: Decimal
    frequency: str
    next_due_date: str
    done: bool
    counted: bool
    due_soon: bool
    updated_on: str | None
    note: str | None
    on_new: bool

    def due_text(self) -> str:
        if not self.due_soon:
            return ""
        return f"Due {short_date(date.fromisoformat(self.next_due_date))}"


@dataclass(frozen=True)
class ReplacementProgress:
    replacement_id: int
    old_id: int
    new_id: int
    done: int
    counted: int
    due_soon_count: int
    phrase: str
    items: tuple[ItemProgress, ...]
    not_counted_names: tuple[str, ...]
    can_finish: bool


def _due_soon(iso: str, today: date, days: int) -> bool:
    try:
        due = date.fromisoformat(iso)
    except ValueError:
        return False
    return due <= today + timedelta(days=days)


def progress(
    db: Session,
    replacement: PaymentMethodReplacement,
    *,
    today: date | None = None,
    due_soon_days: int = REPLACEMENT_DUE_SOON_DAYS,
    sync: bool = True,
) -> ReplacementProgress:
    if sync:
        sync_replacement_items(db, replacement)
    today = _today(today)
    rows = (
        db.query(PaymentMethodReplacementItem, RecurringBill)
        .join(
            RecurringBill,
            PaymentMethodReplacementItem.recurring_bill_id == RecurringBill.id,
        )
        .filter(PaymentMethodReplacementItem.replacement_id == replacement.id)
        .all()
    )
    built: list[ItemProgress] = []
    for item, bill in rows:
        on_new = bill.payment_method_id == replacement.new_id
        on_old = bill.payment_method_id == replacement.old_id
        counted = bool(bill.is_active) and (on_old or on_new)
        done = counted and on_new
        due = (
            counted and not done and _due_soon(bill.next_due_date, today, due_soon_days)
        )
        built.append(
            ItemProgress(
                bill_id=bill.id,
                name=bill.name,
                provider=bill.debtor_provider,
                amount=Decimal(str(bill.amount)),
                frequency=bill.frequency,
                next_due_date=bill.next_due_date,
                done=done,
                counted=counted,
                due_soon=due,
                updated_on=item.updated_on,
                note=item.note,
                on_new=on_new,
            )
        )
    counted_rows = [row for row in built if row.counted]
    todo = sorted(
        (row for row in counted_rows if not row.done),
        key=lambda row: _date_key(row.next_due_date, row.bill_id),
    )
    done_rows = sorted(
        (row for row in counted_rows if row.done),
        key=lambda row: _date_key(row.next_due_date, row.bill_id),
    )
    ordered = tuple(todo + done_rows)
    not_counted = tuple(sorted(row.name for row in built if not row.counted))
    done_count = len(done_rows)
    counted_count = len(counted_rows)
    due_count = sum(1 for row in todo if row.due_soon)
    return ReplacementProgress(
        replacement_id=replacement.id,
        old_id=replacement.old_id,
        new_id=replacement.new_id,
        done=done_count,
        counted=counted_count,
        due_soon_count=due_count,
        phrase=progress_phrase(done_count, counted_count),
        items=ordered,
        not_counted_names=not_counted,
        can_finish=done_count == counted_count and counted_count > 0,
    )


def _require_open(replacement: PaymentMethodReplacement) -> None:
    if replacement.status != "open":
        raise PaymentMethodError("This replacement is not open.", 409)


def _item_for(
    db: Session, replacement: PaymentMethodReplacement, bill_id: int
) -> tuple[PaymentMethodReplacementItem, RecurringBill]:
    row = (
        db.query(PaymentMethodReplacementItem, RecurringBill)
        .join(
            RecurringBill,
            PaymentMethodReplacementItem.recurring_bill_id == RecurringBill.id,
        )
        .filter(
            PaymentMethodReplacementItem.replacement_id == replacement.id,
            PaymentMethodReplacementItem.recurring_bill_id == bill_id,
        )
        .first()
    )
    if row is None:
        raise PaymentMethodError("That bill is not on this list.", 404)
    item, bill = row
    return item, bill


def tick(
    db: Session,
    replacement: PaymentMethodReplacement,
    bill_id: int,
    *,
    today: date | None = None,
) -> None:
    """Move the bill to the new method. A typed date is left as it is."""
    _require_open(replacement)
    sync_replacement_items(db, replacement)
    item, bill = _item_for(db, replacement, bill_id)
    bill.payment_method_id = replacement.new_id
    if not item.updated_on:
        item.updated_on = _today(today).isoformat()


def untick(db: Session, replacement: PaymentMethodReplacement, bill_id: int) -> None:
    """Move the bill back. Clears the date and keeps the note."""
    _require_open(replacement)
    item, bill = _item_for(db, replacement, bill_id)
    bill.payment_method_id = replacement.old_id
    item.updated_on = None


def save_note(
    db: Session,
    replacement: PaymentMethodReplacement,
    bill_id: int,
    *,
    note: str | None,
    updated_on: str | None,
) -> None:
    _require_open(replacement)
    sync_replacement_items(db, replacement)
    parsed = ReplacementNoteInput.model_validate(
        {"note": note, "updated_on": updated_on}
    )
    item, _bill = _item_for(db, replacement, bill_id)
    item.note = parsed.note
    item.updated_on = parsed.updated_on


def start(
    db: Session,
    old_id: int,
    *,
    new_id: int | None = None,
    new_method: dict | None = None,
    explicit_last4: bool = False,
    explicit_expiry: bool = False,
) -> PaymentMethodReplacement:
    old = db.get(PaymentMethod, old_id)
    if old is None:
        raise PaymentMethodError("Payment method not found.", 404)
    if old.is_archived:
        raise PaymentMethodError("Payment method not found or archived.")
    if (
        db.query(PaymentMethodReplacement)
        .filter(
            PaymentMethodReplacement.status == "open",
            PaymentMethodReplacement.new_id == old.id,
        )
        .first()
        is not None
    ):
        raise PaymentMethodError("Finish or stop replacing first.", 409)
    if not _in_use_bills(db, old.id):
        raise PaymentMethodError("No active bills use this. Archive it instead.")
    if (
        db.query(PaymentMethodReplacement)
        .filter(
            PaymentMethodReplacement.status == "open",
            PaymentMethodReplacement.old_id == old.id,
        )
        .first()
        is not None
    ):
        raise PaymentMethodError("A replacement is already open for this.", 409)

    if new_id is not None:
        new = db.get(PaymentMethod, new_id)
        if new is None or new.is_archived:
            raise PaymentMethodError("Payment method not found or archived.")
    else:
        if new_method is None:
            raise PaymentMethodError("Pick a payment method or add a new one.")
        values = prepare_payment_method(
            new_method,
            explicit_last4=explicit_last4,
            explicit_expiry=explicit_expiry,
        )
        new = PaymentMethod(**values)
        db.add(new)
        db.flush()

    if new.id == old.id:
        raise PaymentMethodError("Pick a different payment method.")
    busy = (
        db.query(PaymentMethodReplacement)
        .filter(
            PaymentMethodReplacement.status == "open",
            or_(
                PaymentMethodReplacement.old_id == new.id,
                PaymentMethodReplacement.new_id == new.id,
            ),
        )
        .first()
    )
    if busy is not None:
        raise PaymentMethodError(
            "That payment method is already in a replacement.", 409
        )

    replacement = PaymentMethodReplacement(
        old_id=old.id,
        new_id=new.id,
        status="open",
        started_at=_utcnow(),
    )
    db.add(replacement)
    db.flush()
    for bill in _in_use_bills(db, old.id):
        db.add(
            PaymentMethodReplacementItem(
                replacement_id=replacement.id,
                recurring_bill_id=bill.id,
            )
        )
    db.flush()
    return replacement


def finish(db: Session, replacement: PaymentMethodReplacement) -> PaymentMethod:
    _require_open(replacement)
    sync_replacement_items(db, replacement)
    still = (
        db.query(RecurringBill)
        .filter(
            RecurringBill.is_active == True,
            RecurringBill.payment_method_id == replacement.old_id,
        )
        .count()
    )
    if still:
        raise PaymentMethodError("A bill is still on the old payment method.", 409)
    replacement.status = "finished"
    replacement.ended_at = _utcnow()
    old = db.get(PaymentMethod, replacement.old_id)
    if old is None:
        raise PaymentMethodError("Payment method not found.", 404)
    old.is_archived = True
    return old


def stop(db: Session, replacement: PaymentMethodReplacement) -> None:
    _require_open(replacement)
    replacement.status = "stopped"
    replacement.ended_at = _utcnow()


@dataclass(frozen=True)
class PaymentAlertLine:
    method_id: int
    rank: int
    text: str
    href: str
    link_label: str
    due_soon: bool


@dataclass(frozen=True)
class PaymentAlerts:
    headline: str
    warning: bool
    lines: tuple[PaymentAlertLine, ...]
    extra: int

    @property
    def show_banner(self) -> bool:
        return bool(self.lines)


def _phrase_with_due(prog: ReplacementProgress) -> str:
    if prog.due_soon_count:
        return f"{prog.phrase}, {prog.due_soon_count} due soon"
    return prog.phrase


def _still_sentence(count: int) -> str:
    if count == 1:
        return "and 1 bill is still on it"
    return f"and {count} bills are still on it"


def _archived_sentence(label: str, bills: list[RecurringBill]) -> str:
    names = _join_names([bill.name for bill in _sorted_bills(bills)])
    if len(bills) == 1:
        return f"{label} is archived but {names} still uses it."
    return f"{label} is archived but {names} still use it."


def compute_payment_alerts(
    db: Session, *, today: date | None = None, warn_days: int | None = None
) -> PaymentAlerts:
    """One dashboard line per method that needs a look. Unused methods are skipped."""
    today = _today(today)
    warn = _warn(warn_days)
    opens = open_replacements(db)
    progress_by_old: dict[int, ReplacementProgress] = {}
    rep_by_old: dict[int, PaymentMethodReplacement] = {}
    for rep in opens:
        rep_by_old[rep.old_id] = rep
        progress_by_old[rep.old_id] = progress(db, rep, today=today)

    methods = {method.id: method for method in db.query(PaymentMethod).all()}
    active = (
        db.query(RecurringBill)
        .filter(
            RecurringBill.is_active == True,
            RecurringBill.payment_method_id.is_not(None),
        )
        .all()
    )
    by_method: dict[int, list[RecurringBill]] = {}
    for bill in active:
        if bill.payment_method_id is None:
            continue
        by_method.setdefault(bill.payment_method_id, []).append(bill)

    lines: list[PaymentAlertLine] = []
    for method in methods.values():
        used = by_method.get(method.id, [])
        rep = rep_by_old.get(method.id)
        if rep is not None:
            prog = progress_by_old[method.id]
            if prog.counted == 0 and not used:
                continue
            new = methods.get(rep.new_id)
            new_label = method_label(new) if new is not None else "the new method"
            old_label = method_label(method)
            status = expiry_status(method, today, warn)
            phrase = _phrase_with_due(prog)
            if status == "expired" and method.expiry_month and method.expiry_year:
                rank = 0
                when = end_of_phrase(method.expiry_month, method.expiry_year)
                still = prog.counted - prog.done
                if still:
                    text = (
                        f"{old_label} expired at the {when} {_still_sentence(still)}. "
                        f"Moving to {new_label}: {phrase}."
                    )
                else:
                    text = (
                        f"{old_label} expired at the {when}. "
                        f"Moving to {new_label}: {phrase}."
                    )
            elif status == "expiring" and method.expiry_month and method.expiry_year:
                rank = 1
                text = (
                    f"Replacing {old_label} (expires "
                    f"{end_of_phrase(method.expiry_month, method.expiry_year)}) "
                    f"with {new_label}: {phrase}."
                )
            else:
                rank = 3
                text = f"Replacing {old_label} with {new_label}: {phrase}."
            lines.append(
                PaymentAlertLine(
                    method_id=method.id,
                    rank=rank,
                    text=text,
                    href=f"/payment-methods/{method.id}/replace",
                    link_label="Continue",
                    due_soon=prog.due_soon_count > 0,
                )
            )
            continue
        if not used:
            continue
        if method.is_archived:
            lines.append(
                PaymentAlertLine(
                    method_id=method.id,
                    rank=2,
                    text=_archived_sentence(method_label(method), used),
                    href=f"/bills?payment_method={method.id}",
                    link_label="Edit bills",
                    due_soon=False,
                )
            )
            continue
        status = expiry_status(method, today, warn)
        if status not in ("expired", "expiring"):
            continue
        if method.expiry_month is None or method.expiry_year is None:
            continue
        names = _join_names([bill.name for bill in _sorted_bills(used)])
        when = end_of_phrase(method.expiry_month, method.expiry_year)
        label = method_label(method)
        if status == "expired":
            text = (
                f"{label} ({method.owner}) expired at the {when}. "
                f"Still used by {names}."
            )
            rank = 0
        else:
            text = f"{label} ({method.owner}) expires at the {when}. Used by {names}."
            rank = 1
        lines.append(
            PaymentAlertLine(
                method_id=method.id,
                rank=rank,
                text=text,
                href=f"/payment-methods/{method.id}/replace",
                link_label="Replace",
                due_soon=False,
            )
        )

    lines.sort(key=lambda line: (line.rank, line.text, line.method_id))
    extra = max(0, len(lines) - LINE_CAP)
    visible = tuple(lines[:LINE_CAP])
    warning = any(line.rank <= 2 or line.due_soon for line in lines)
    if not lines:
        headline = ""
    elif warning:
        headline = "Payment updates needed"
    else:
        headline = "Payment updates in progress"
    return PaymentAlerts(
        headline=headline,
        warning=warning,
        lines=visible,
        extra=extra,
    )
