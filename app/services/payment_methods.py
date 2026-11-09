"""Payment methods, expiry alerts, and saved card-replacement checklists.

One rule set for the web UI, the JSON API, and the MCP tools. A replacement
is a saved job: it changes only when someone ticks, finishes, or stops it.
"""

from __future__ import annotations

import calendar
import re
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
    PaymentMethodResponse,
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
    "wallet": "Wallet",
}

PROVIDER_LABELS = {
    "apple_pay": "Apple Pay",
    "google_pay": "Google Pay",
}

# YYYY-MM from <input type="month">, or a typed MM/YY, MM/YYYY, MM-YY, MM-YYYY.
_EXPIRY_INPUT = re.compile(
    r"^(?:(?P<iso_year>[0-9]{4})-(?P<iso_month>[0-9]{1,2})"
    r"|(?P<month>[0-9]{1,2})[/-](?P<year>[0-9]{4}|[0-9]{2}))$"
)
_EXPIRY_INPUT_MESSAGE = "Use MM/YY or pick a month."


class PaymentMethodError(Exception):
    """A rule failure with a message safe to show. The message has no card number."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def method_label(method: PaymentMethod) -> str:
    if method.kind == "wallet":
        provider = PROVIDER_LABELS.get(method.wallet_provider or "", "Wallet")
        card = method.funding_method
        if card is None or card.kind == "wallet":
            return provider
        return f"{provider} ({method_label(card)})"
    if method.last4:
        return f"{method.nickname} ending {method.last4}"
    return method.nickname


def choice_label(method: PaymentMethod) -> str:
    """Bill picker and replacement dropdown. Wallets use the card's last 4."""
    if method.kind == "wallet":
        return method_label(method)
    suffix = f" ending {method.last4}" if method.last4 else ""
    return f"{method.owner} — {method.nickname}{suffix}"


def expires_on(month: int, year: int) -> date:
    """Last calendar day the card is still valid."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, last)


def effective_expiry(method: PaymentMethod) -> tuple[int, int] | None:
    """Month and year behind every expiry display.

    A card uses its own expiry. A wallet uses the card it is linked to.
    The banner, the Expires column, the list badge, and the bill badge all
    go through this.
    """
    card = method
    if method.kind == "wallet":
        funded = method.funding_method
        if funded is None or funded.kind != "card":
            return None
        card = funded
    elif method.kind != "card":
        return None
    if card.expiry_month is None or card.expiry_year is None:
        return None
    return card.expiry_month, card.expiry_year


def expiry_status(method: PaymentMethod, today: date, warn_days: int) -> str | None:
    """``expired``, ``expiring``, or None. Wallets follow the linked card."""
    pair = effective_expiry(method)
    if pair is None:
        return None
    month, year = pair
    end = expires_on(month, year)
    if today > end:
        return "expired"
    if end <= today + timedelta(days=warn_days):
        return "expiring"
    return None


def expires_label(method: PaymentMethod) -> str:
    pair = effective_expiry(method)
    if pair is None:
        return "—"
    return end_of_phrase(pair[0], pair[1])


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


def replacement_phrase(
    bill_done: int,
    bill_counted: int,
    wallet_done: int,
    wallet_counted: int,
    wallet_noun: str,
) -> str:
    """Bills keep the old sentence. Wallets are "N of M moved", or both."""
    if wallet_counted == 0:
        return progress_phrase(bill_done, bill_counted)
    wallet_part = f"{wallet_done} of {wallet_counted} moved"
    if bill_counted == 0:
        return wallet_part
    return (
        f"{progress_phrase(bill_done, bill_counted)}, "
        f"{wallet_done} of {wallet_counted} {wallet_noun} moved"
    )


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


def _any_funding_wallet(db: Session, method_id: int) -> bool:
    """True when any wallet, archived or not, is funded by this method."""
    return (
        db.query(PaymentMethod.id)
        .filter(PaymentMethod.funding_method_id == method_id)
        .first()
        is not None
    )


def funded_wallets(db: Session, method_id: int) -> list[PaymentMethod]:
    """Non-archived wallets whose funding card is this method."""
    return (
        db.query(PaymentMethod)
        .filter(
            PaymentMethod.funding_method_id == method_id,
            PaymentMethod.kind == "wallet",
            PaymentMethod.is_archived == False,
        )
        .order_by(PaymentMethod.id)
        .all()
    )


def prepare_payment_method(
    data: dict,
    *,
    explicit_last4: bool,
    explicit_expiry: bool,
    explicit_provider: bool = False,
    explicit_funding: bool = False,
    db: Session | None = None,
    existing: PaymentMethod | None = None,
) -> dict:
    """Validate a create payload, or a patch merged onto the stored row.

    An explicit non-null expiry on a non-card, or last 4 digits on a manual
    method or a wallet, is rejected. A wallet stores no expiry and no last 4.
    Re-posting the wallet's current funding card is allowed after that card
    is archived. A nickname edit keeps the provider and the link.
    """
    parsed = PaymentMethodCreate.model_validate(data)
    kind = parsed.kind.value
    last4 = parsed.last4
    month = parsed.expiry_month
    year = parsed.expiry_year
    provider = parsed.wallet_provider.value if parsed.wallet_provider else None
    funding_id = parsed.funding_method_id
    if (
        existing is not None
        and existing.kind == PaymentMethodKind.card.value
        and kind != PaymentMethodKind.card.value
        and db is not None
        and _any_funding_wallet(db, existing.id)
    ):
        raise PaymentMethodError("A wallet still links to this.", 409)
    if kind != PaymentMethodKind.card.value:
        if explicit_expiry and (month is not None or year is not None):
            raise PaymentMethodError("Only a card has an expiry.")
        month = None
        year = None
    if kind == PaymentMethodKind.manual.value:
        if explicit_last4 and last4 is not None:
            raise PaymentMethodError("This type doesn't use last 4 digits.")
        last4 = None
    if kind == PaymentMethodKind.wallet.value:
        if explicit_last4 and last4 is not None:
            raise PaymentMethodError("A wallet doesn't have its own last 4 digits.")
        last4 = None
    if kind == PaymentMethodKind.card.value and (
        last4 is None or month is None or year is None
    ):
        raise PaymentMethodError("A card needs the last 4 digits and an expiry.")
    if kind != PaymentMethodKind.wallet.value:
        if explicit_provider and provider is not None:
            raise PaymentMethodError("Only a wallet has a provider.")
        if explicit_funding and funding_id is not None:
            raise PaymentMethodError("Only a wallet links to a card.")
        provider = None
        funding_id = None
    else:
        if provider not in ("apple_pay", "google_pay"):
            raise PaymentMethodError("Pick Apple Pay or Google Pay.")
        funding_id = _funding_card_id(db, funding_id, existing=existing)
    return {
        "nickname": parsed.nickname,
        "kind": kind,
        "last4": last4,
        "expiry_month": month,
        "expiry_year": year,
        "owner": parsed.owner,
        "wallet_provider": provider,
        "funding_method_id": funding_id,
    }


def _funding_card_id(
    db: Session | None,
    funding_id: int | None,
    *,
    existing: PaymentMethod | None,
) -> int:
    if funding_id is None or db is None:
        raise PaymentMethodError("Link the wallet to a card.")
    card = db.get(PaymentMethod, funding_id)
    if card is None or card.kind != "card":
        raise PaymentMethodError("Link the wallet to a card.")
    if existing is not None and card.id == existing.id:
        raise PaymentMethodError("A wallet can't link to itself.")
    same = existing is not None and existing.funding_method_id == funding_id
    if card.is_archived and not same:
        raise PaymentMethodError("That card is archived.")
    return card.id


def merge_payment_method(existing: PaymentMethod, patch: dict) -> dict:
    data = {
        "nickname": existing.nickname,
        "kind": existing.kind,
        "last4": existing.last4,
        "expiry_month": existing.expiry_month,
        "expiry_year": existing.expiry_year,
        "owner": existing.owner,
        "wallet_provider": existing.wallet_provider,
        "funding_method_id": existing.funding_method_id,
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
    method.wallet_provider = values["wallet_provider"]
    method.funding_method_id = values["funding_method_id"]


def serialize_payment_method(method: PaymentMethod) -> dict:
    """JSON shape shared by the API and MCP, including the card expiry a wallet uses."""
    pair = effective_expiry(method)
    payload = PaymentMethodResponse.model_validate(method)
    payload.effective_expiry_month = pair[0] if pair is not None else None
    payload.effective_expiry_year = pair[1] if pair is not None else None
    return payload.model_dump(mode="json")


def parse_month_input(raw: str) -> tuple[int, int]:
    """Parse a card expiry into ``(month, year)``.

    Accepts ``YYYY-MM`` from ``<input type="month">``, and typed ``MM/YY``,
    ``MM/YYYY``, ``MM-YY``, and ``MM-YYYY``. A single-digit month is allowed.
    Two-digit years are 2000–2099.
    """
    match = _EXPIRY_INPUT.fullmatch(raw.strip())
    if match is None:
        raise PaymentMethodError(_EXPIRY_INPUT_MESSAGE)
    iso_year = match.group("iso_year")
    if iso_year is not None:
        year = int(iso_year)
        month = int(match.group("iso_month") or "0")
    else:
        month = int(match.group("month") or "0")
        year_text = match.group("year") or ""
        year = int(year_text) if year_text else 0
        if len(year_text) == 2:
            year += 2000
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise PaymentMethodError(_EXPIRY_INPUT_MESSAGE)
    return month, year


@dataclass(frozen=True)
class FormPayload:
    data: dict
    explicit_last4: bool
    explicit_expiry: bool
    explicit_provider: bool
    explicit_funding: bool


def form_to_payload(form, existing: PaymentMethod | None) -> FormPayload:
    """Read a web form. Disabled fields are absent and keep the stored value."""
    data = {
        "nickname": existing.nickname if existing is not None else "",
        "kind": existing.kind if existing is not None else "",
        "owner": existing.owner if existing is not None else "",
        "last4": existing.last4 if existing is not None else None,
        "expiry_month": existing.expiry_month if existing is not None else None,
        "expiry_year": existing.expiry_year if existing is not None else None,
        "wallet_provider": existing.wallet_provider if existing is not None else None,
        "funding_method_id": (
            existing.funding_method_id if existing is not None else None
        ),
    }
    if "nickname" in form:
        data["nickname"] = str(form.get("nickname") or "")
    if "owner" in form:
        data["owner"] = str(form.get("owner") or "")
    if "kind" in form:
        data["kind"] = str(form.get("kind") or "")
    explicit_last4 = False
    explicit_expiry = False
    explicit_provider = False
    explicit_funding = False
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
    if "wallet_provider" in form:
        raw = str(form.get("wallet_provider") or "").strip()
        data["wallet_provider"] = raw or None
        explicit_provider = raw != ""
    elif existing is None:
        data["wallet_provider"] = None
    if "funding_method_id" in form:
        raw = str(form.get("funding_method_id") or "").strip()
        explicit_funding = raw != ""
        if raw == "":
            data["funding_method_id"] = None
        else:
            try:
                data["funding_method_id"] = int(raw)
            except ValueError:
                raise PaymentMethodError("Link the wallet to a card.") from None
    elif existing is None:
        data["funding_method_id"] = None
    return FormPayload(
        data=data,
        explicit_last4=explicit_last4,
        explicit_expiry=explicit_expiry,
        explicit_provider=explicit_provider,
        explicit_funding=explicit_funding,
    )


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
                kind=item.kind,
                bill_id=item.bill_id,
                name=item.name,
                done=item.done,
                due_soon=item.due_soon,
                updated_on=item.updated_on,
                note=item.note,
                wallet_method_id=item.wallet_method_id,
                wallet_provider=item.wallet_provider,
                funding_method_id=item.funding_method_id,
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
    if _any_funding_wallet(db, method.id):
        raise PaymentMethodError("A wallet still links to this.", 409)
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
    pointed = (
        db.query(PaymentMethodReplacementItem)
        .filter(PaymentMethodReplacementItem.wallet_method_id == method.id)
        .all()
    )
    for item in pointed:
        db.delete(item)
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


def funding_card_choices(
    db: Session,
    *,
    current_id: int | None = None,
    editing_id: int | None = None,
) -> list[PaymentMethod]:
    """Non-archived cards a wallet can link to, plus the current card if archived."""
    cards = (
        db.query(PaymentMethod)
        .filter(PaymentMethod.kind == "card", PaymentMethod.is_archived == False)
        .all()
    )
    if editing_id is not None:
        cards = [card for card in cards if card.id != editing_id]
    ids = {card.id for card in cards}
    if current_id is not None and current_id not in ids:
        current = db.get(PaymentMethod, current_id)
        if current is not None and current.kind == "card":
            cards.append(current)
    cards.sort(key=lambda card: (card.owner.lower(), card.nickname.lower(), card.id))
    return cards


def also_used_by(db: Session, method: PaymentMethod) -> str | None:
    """Card detail line. Archived wallets are left out."""
    if method.kind != "card":
        return None
    wallets = funded_wallets(db, method.id)
    if not wallets:
        return None
    names = [
        PROVIDER_LABELS.get(wallet.wallet_provider or "", "Wallet")
        for wallet in wallets
    ]
    return f"Also used by: {_join_names(names)}"


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


def _add_wallet_item(db: Session, replacement_id: int, wallet_id: int) -> None:
    """Insert a wallet checklist row. A duplicate from a concurrent load is skipped."""
    item = PaymentMethodReplacementItem(
        replacement_id=replacement_id,
        wallet_method_id=wallet_id,
    )
    try:
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError:
        return


def sync_replacement_items(db: Session, replacement: PaymentMethodReplacement) -> None:
    """Add a row for every in-use bill, and every non-archived wallet, still on the old card.

    A duplicate insert (two loads at once) is skipped. Wallets are never
    relinked here: the tick is the user updating Wallet on their phone.
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
        .filter(
            PaymentMethodReplacementItem.replacement_id == replacement.id,
            PaymentMethodReplacementItem.recurring_bill_id.is_not(None),
        )
        .all()
    }
    for bill in bills:
        if bill.id in existing:
            continue
        _add_replacement_item(db, replacement.id, bill.id)
        existing.add(bill.id)
    existing_wallets = {
        wallet_id
        for (wallet_id,) in db.query(PaymentMethodReplacementItem.wallet_method_id)
        .filter(
            PaymentMethodReplacementItem.replacement_id == replacement.id,
            PaymentMethodReplacementItem.wallet_method_id.is_not(None),
        )
        .all()
    }
    for wallet in funded_wallets(db, replacement.old_id):
        if wallet.id in existing_wallets:
            continue
        _add_wallet_item(db, replacement.id, wallet.id)
        existing_wallets.add(wallet.id)


@dataclass(frozen=True)
class ItemProgress:
    kind: str
    bill_id: int | None
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
    wallet_method_id: int | None = None
    wallet_provider: str | None = None
    funding_method_id: int | None = None

    def due_text(self) -> str:
        if not self.due_soon or not self.next_due_date:
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
    bill_counted: int = 0
    wallet_counted: int = 0


def _due_soon(iso: str, today: date, days: int) -> bool:
    try:
        due = date.fromisoformat(iso)
    except ValueError:
        return False
    return due <= today + timedelta(days=days)


def _wallet_noun(providers: set[str], counted: int) -> str:
    if providers == {"apple_pay"}:
        return "Apple Pay"
    if providers == {"google_pay"}:
        return "Google Pay"
    if counted == 1:
        return "wallet"
    return "wallets"


def _wallet_instruction(new_label: str, provider: str | None) -> str:
    name = PROVIDER_LABELS.get(provider or "", "Wallet")
    return f"Add {new_label} to {name} on your phone, then tick"


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
        db.query(PaymentMethodReplacementItem)
        .filter(PaymentMethodReplacementItem.replacement_id == replacement.id)
        .all()
    )
    new = db.get(PaymentMethod, replacement.new_id)
    new_label = method_label(new) if new is not None else "the new card"
    built: list[ItemProgress] = []
    for item in rows:
        if item.recurring_bill_id is not None:
            bill = item.recurring_bill
            if bill is None:
                continue
            on_new = bill.payment_method_id == replacement.new_id
            on_old = bill.payment_method_id == replacement.old_id
            counted = bool(bill.is_active) and (on_old or on_new)
            done = counted and on_new
            due = (
                counted
                and not done
                and _due_soon(bill.next_due_date, today, due_soon_days)
            )
            built.append(
                ItemProgress(
                    kind="bill",
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
            continue
        wallet = item.wallet_method
        if wallet is None:
            continue
        on_new = wallet.funding_method_id == replacement.new_id
        on_old = wallet.funding_method_id == replacement.old_id
        counted = (
            wallet.kind == "wallet" and not wallet.is_archived and (on_old or on_new)
        )
        done = counted and on_new
        provider_name = PROVIDER_LABELS.get(wallet.wallet_provider or "", "Wallet")
        built.append(
            ItemProgress(
                kind="wallet",
                bill_id=None,
                name=_wallet_instruction(new_label, wallet.wallet_provider),
                provider=provider_name,
                amount=Decimal(0),
                frequency="",
                next_due_date="",
                done=done,
                counted=counted,
                due_soon=False,
                updated_on=item.updated_on,
                note=item.note,
                on_new=on_new,
                wallet_method_id=wallet.id,
                wallet_provider=wallet.wallet_provider,
                funding_method_id=wallet.funding_method_id,
            )
        )
    bill_rows = [row for row in built if row.kind == "bill"]
    wallet_rows = [row for row in built if row.kind == "wallet"]
    counted_bills = [row for row in bill_rows if row.counted]
    counted_wallets = [row for row in wallet_rows if row.counted]
    todo_bills = sorted(
        (row for row in counted_bills if not row.done),
        key=lambda row: _date_key(row.next_due_date, row.bill_id or 0),
    )
    done_bills = sorted(
        (row for row in counted_bills if row.done),
        key=lambda row: _date_key(row.next_due_date, row.bill_id or 0),
    )
    todo_wallets = sorted(
        (row for row in counted_wallets if not row.done),
        key=lambda row: row.wallet_method_id or 0,
    )
    done_wallets = sorted(
        (row for row in counted_wallets if row.done),
        key=lambda row: row.wallet_method_id or 0,
    )
    ordered = tuple(todo_bills + todo_wallets + done_bills + done_wallets)
    not_counted = tuple(
        sorted(
            row.name if row.kind == "bill" else row.provider
            for row in built
            if not row.counted
        )
    )
    bill_done = len(done_bills)
    bill_counted = len(counted_bills)
    wallet_done = len(done_wallets)
    wallet_counted = len(counted_wallets)
    done_count = bill_done + wallet_done
    counted_count = bill_counted + wallet_counted
    providers = {row.wallet_provider for row in counted_wallets if row.wallet_provider}
    due_count = sum(1 for row in todo_bills if row.due_soon)
    return ReplacementProgress(
        replacement_id=replacement.id,
        old_id=replacement.old_id,
        new_id=replacement.new_id,
        done=done_count,
        counted=counted_count,
        due_soon_count=due_count,
        phrase=replacement_phrase(
            bill_done,
            bill_counted,
            wallet_done,
            wallet_counted,
            _wallet_noun(providers, wallet_counted),
        ),
        items=ordered,
        not_counted_names=not_counted,
        can_finish=done_count == counted_count and counted_count > 0,
        bill_counted=bill_counted,
        wallet_counted=wallet_counted,
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


def _wallet_item_for(
    db: Session, replacement: PaymentMethodReplacement, wallet_id: int
) -> tuple[PaymentMethodReplacementItem, PaymentMethod]:
    item = (
        db.query(PaymentMethodReplacementItem)
        .filter(
            PaymentMethodReplacementItem.replacement_id == replacement.id,
            PaymentMethodReplacementItem.wallet_method_id == wallet_id,
        )
        .first()
    )
    wallet = db.get(PaymentMethod, wallet_id)
    if item is None or wallet is None or wallet.kind != "wallet":
        raise PaymentMethodError("That wallet is not on this list.", 404)
    return item, wallet


def tick_wallet(
    db: Session,
    replacement: PaymentMethodReplacement,
    wallet_id: int,
    *,
    today: date | None = None,
) -> None:
    """Repoint the wallet at the new card. Nothing else relinks it."""
    _require_open(replacement)
    sync_replacement_items(db, replacement)
    item, wallet = _wallet_item_for(db, replacement, wallet_id)
    new = db.get(PaymentMethod, replacement.new_id)
    if new is None or new.is_archived or new.kind != "card":
        raise PaymentMethodError("Pick a card. Wallets stay linked to a card.")
    wallet.funding_method_id = replacement.new_id
    if not item.updated_on:
        item.updated_on = _today(today).isoformat()


def untick_wallet(
    db: Session, replacement: PaymentMethodReplacement, wallet_id: int
) -> None:
    """Point the wallet back at the old card. Clears the date and keeps the note."""
    _require_open(replacement)
    item, wallet = _wallet_item_for(db, replacement, wallet_id)
    wallet.funding_method_id = replacement.old_id
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
    bills = _in_use_bills(db, old.id)
    wallets = funded_wallets(db, old.id)
    if not bills and not wallets:
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
            explicit_provider=new_method.get("wallet_provider") is not None,
            explicit_funding=new_method.get("funding_method_id") is not None,
            db=db,
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
    if wallets and new.kind != "card":
        raise PaymentMethodError("Pick a card. Wallets stay linked to a card.")

    replacement = PaymentMethodReplacement(
        old_id=old.id,
        new_id=new.id,
        status="open",
        started_at=_utcnow(),
    )
    db.add(replacement)
    db.flush()
    for bill in bills:
        db.add(
            PaymentMethodReplacementItem(
                replacement_id=replacement.id,
                recurring_bill_id=bill.id,
            )
        )
    for wallet in wallets:
        db.add(
            PaymentMethodReplacementItem(
                replacement_id=replacement.id,
                wallet_method_id=wallet.id,
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
    still_wallets = len(funded_wallets(db, replacement.old_id))
    if still_wallets:
        raise PaymentMethodError("A wallet is still linked to the old card.", 409)
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
            pair = effective_expiry(method)
            phrase = _phrase_with_due(prog)
            still = sum(
                1
                for item in prog.items
                if item.kind == "bill" and item.counted and not item.done
            )
            if status == "expired" and pair is not None:
                rank = 0
                when = end_of_phrase(pair[0], pair[1])
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
            elif status == "expiring" and pair is not None:
                rank = 1
                text = (
                    f"Replacing {old_label} (expires "
                    f"{end_of_phrase(pair[0], pair[1])}) "
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
        funding = method.funding_method if method.kind == "wallet" else None
        if method.is_archived or (funding is not None and funding.is_archived):
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
        pair = effective_expiry(method)
        if status not in ("expired", "expiring") or pair is None:
            continue
        names = _join_names([bill.name for bill in _sorted_bills(used)])
        when = end_of_phrase(pair[0], pair[1])
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
