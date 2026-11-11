"""JSON-lines audit log for successful REST and MCP writes.

One line per successful call goes to stdout through the ``glow_worm.audit``
logger. That logger does not propagate, so uvicorn does not add a prefix.
A failure while building or writing a line is logged on the ``app`` logger
and never changes the HTTP or MCP response.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import inspect as sa_inspect

from app.services.money import money

AUDIT_LOGGER_NAME = "glow_worm.audit"
REDACTED = "[redacted]"

# Never write these field names. ``password`` / ``password_hash`` are recorded
# under the name ``password`` with both sides redacted, so a password change
# is visible without the secret.
_PASSWORD_FIELDS = frozenset({"password", "password_hash"})
_DROP_FIELDS = frozenset(
    {
        "key",
        "key_hash",
        "last4",
        "email",
        "secret",
        "secret_key",
        "authorization",
        "token",
        "api_key",
        "plain_key",
        "raw_key",
    }
)
_OMIT_FROM_CHANGES = frozenset({"updated_at"})

# Short identifying fields for creates and deletes. Not a full row.
SUMMARY_FIELDS: dict[str, tuple[str, ...]] = {
    "transaction": ("id", "date", "amount", "description", "type"),
    "bill": ("id", "name", "amount", "next_due_date"),
    "budget": ("id", "category_id", "month", "year", "allocated_amount"),
    "category": ("id", "name", "type"),
    "contract": ("id", "name", "provider"),
    "income_allocation": ("id", "monthly_income_amount"),
    "secondary_income_allocation": ("id", "label"),
    "secondary_income": ("id", "date", "amount"),
    "payment_method": ("id", "nickname", "kind", "owner"),
    "sinking_fund": ("id", "name"),
    "user": ("id", "username"),
    "api_key": ("id", "name", "user_id"),
}

_audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
_failure_logger = logging.getLogger("app")

_current_actor: contextvars.ContextVar[AuditActor | None] = contextvars.ContextVar(
    "audit_actor", default=None
)


class AuditStreamHandler(logging.StreamHandler):
    """Stdout handler that writes the log message unchanged."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)
        self.setLevel(logging.INFO)
        self.setFormatter(logging.Formatter("%(message)s"))


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Identity copied at auth time, before the middleware session closes."""

    username: str
    user_id: int | None
    auth_method: str
    api_key_id: int | None = None
    api_key_name: str | None = None


def configure_audit_logging() -> None:
    """Attach one propagate=False stdout handler. Safe to call more than once."""
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(isinstance(handler, AuditStreamHandler) for handler in logger.handlers):
        logger.addHandler(AuditStreamHandler())


def get_current_actor() -> AuditActor | None:
    return _current_actor.get()


def set_current_actor(actor: AuditActor | None) -> contextvars.Token[AuditActor | None]:
    return _current_actor.set(actor)


def reset_current_actor(token: contextvars.Token[AuditActor | None]) -> None:
    _current_actor.reset(token)


def audit_write(fn):
    """Mark a handler covered. Does not emit a log line."""
    fn.__audit_covered__ = True
    return fn


def snapshot_columns(entity: object) -> dict[str, Any]:
    """Copy mapped column values into a plain dict. No relationships."""
    inspected = sa_inspect(entity)
    if inspected is None:
        return {}
    return {
        attr.key: getattr(entity, attr.key) for attr in inspected.mapper.column_attrs
    }


def serialize_audit_value(value: object) -> object:
    """JSON value for one column. ``None`` stays null; money is never a float."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        else:
            value = value.astimezone(UTC)
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal | float):
        return str(money(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return value
    return str(value)


def _same(old: object, new: object) -> bool:
    if old == new:
        return True
    if isinstance(old, bool) or isinstance(new, bool):
        return False
    if isinstance(old, int | float | Decimal) and isinstance(
        new, int | float | Decimal
    ):
        try:
            return money(old) == money(new)
        except ArithmeticError, ValueError, TypeError:
            return False
    return False


def column_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, dict[str, object]]:
    """Diff mapped columns. Omits ``updated_at`` and unchanged fields."""
    changes: dict[str, dict[str, object]] = {}
    password_recorded = False
    for key in sorted(set(before) | set(after)):
        if key in _OMIT_FROM_CHANGES:
            continue
        old = before.get(key)
        new = after.get(key)
        if _same(old, new):
            continue
        if key in _PASSWORD_FIELDS:
            if not password_recorded:
                changes["password"] = {"old": REDACTED, "new": REDACTED}
                password_recorded = True
            continue
        if key in _DROP_FIELDS:
            continue
        changes[key] = {
            "old": serialize_audit_value(old),
            "new": serialize_audit_value(new),
        }
    return changes


def summary_for(entity_type: str, data: dict[str, Any]) -> dict[str, object]:
    """Short redacted summary of the identifying fields for this entity."""
    fields = SUMMARY_FIELDS.get(entity_type, ("id",))
    summary: dict[str, object] = {}
    for field in fields:
        if field not in data:
            continue
        if field in _DROP_FIELDS or field in _PASSWORD_FIELDS:
            continue
        summary[field] = serialize_audit_value(data[field])
    return summary


def related_rows(
    entity_type: str, entity_ids: list[int], action: str
) -> list[dict[str, object]]:
    return [
        {"entity_type": entity_type, "entity_id": entity_id, "action": action}
        for entity_id in entity_ids
    ]


def _redacted_password(value: object) -> object:
    if isinstance(value, dict) and ("old" in value or "new" in value):
        return {"old": REDACTED, "new": REDACTED}
    return REDACTED


def _json_ready(value: object) -> object:
    """Serialise and apply the denylist again, in case a caller passed raw columns."""
    if isinstance(value, dict):
        ready: dict[str, object] = {}
        password_seen = False
        for raw_key, item in value.items():
            key = str(raw_key)
            if key in _OMIT_FROM_CHANGES or key in _DROP_FIELDS:
                continue
            if key in _PASSWORD_FIELDS:
                if not password_seen:
                    ready["password"] = _redacted_password(item)
                    password_seen = True
                continue
            ready[key] = _json_ready(item)
        return ready
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, str | int):
        return value
    return serialize_audit_value(value)


def _write_audit_line(line: str) -> None:
    _audit_logger.info(line)


def emit_audit(
    *,
    source: str,
    action: str,
    entity_type: str,
    entity_id: int,
    endpoint: str,
    changes: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    related: list[dict[str, object]] | None = None,
    actor: AuditActor | None = None,
) -> None:
    """Write one audit line. Swallows every error."""
    try:
        current = actor if actor is not None else get_current_actor()
        if current is None:
            username: str | None = "unknown"
            user_id = None
            api_key_id = None
            api_key_name = None
            auth_method = None
        else:
            username = current.username or "unknown"
            user_id = current.user_id
            api_key_id = current.api_key_id
            api_key_name = current.api_key_name
            auth_method = current.auth_method
        payload: dict[str, object] = {
            "event": "glow_worm.audit",
            "ts": serialize_audit_value(datetime.now(UTC)),
            "source": source,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "user": username,
            "user_id": user_id,
            "api_key_id": api_key_id,
            "api_key_name": api_key_name,
            "auth_method": auth_method,
            "endpoint": endpoint,
        }
        if changes is not None:
            payload["changes"] = _json_ready(changes)
        if values is not None:
            payload["values"] = _json_ready(values)
        if related:
            payload["related"] = _json_ready(related)
        _write_audit_line(json.dumps(payload, ensure_ascii=False))
    except Exception:
        _failure_logger.exception("audit log failed")


def emit_rest(
    request: object,
    *,
    action: str,
    entity_type: str,
    entity_id: int,
    changes: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    related: list[dict[str, object]] | None = None,
) -> None:
    """Emit after the REST handler's outermost commit. ``request`` is a Starlette request."""
    path = getattr(getattr(request, "url", None), "path", "")
    emit_audit(
        source="rest",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        endpoint=str(path),
        changes=changes,
        values=values,
        related=related,
    )


def emit_mcp(
    *,
    action: str,
    entity_type: str,
    entity_id: int,
    endpoint: str,
    changes: dict[str, Any] | None = None,
    values: dict[str, Any] | None = None,
    related: list[dict[str, object]] | None = None,
) -> None:
    emit_audit(
        source="mcp",
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        endpoint=endpoint,
        changes=changes,
        values=values,
        related=related,
    )
