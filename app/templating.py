from collections.abc import MutableMapping
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from typing import Any, cast

from fastapi.templating import Jinja2Templates

from app.services.contracts import contract_choice_label, contract_label
from app.services.payment_methods import choice_label
from app.services.recurring_payments import (
    format_due_date,
    format_month_year,
    needs_confirm,
    source_is_broken,
    source_label,
)

templates = Jinja2Templates(directory="app/templates")


def _money_format(value) -> str:
    """Format a number with commas and 2 decimal places (e.g. 10,000.00)."""
    try:
        return f"{float(value):,.2f}"
    except ValueError, TypeError:
        return "0.00"


def _qty_format(value) -> str:
    """Trim trailing zeros from a share quantity or price."""
    try:
        text = format(Decimal(str(value)), "f")
    except InvalidOperation, ValueError, TypeError:
        return str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


try:
    _app_version = version("glow-worm")
except PackageNotFoundError:
    _app_version = "dev"


def _due_label(value) -> str:
    try:
        return format_due_date(value)
    except ValueError, TypeError:
        return str(value)


def _month_year(value) -> str:
    try:
        return format_month_year(value)
    except ValueError, TypeError:
        return str(value)


cast(MutableMapping[str, Any], templates.env.filters)["money"] = _money_format
cast(MutableMapping[str, Any], templates.env.filters)["qty"] = _qty_format
cast(MutableMapping[str, Any], templates.env.filters)["due_label"] = _due_label
cast(MutableMapping[str, Any], templates.env.filters)["month_year"] = _month_year
cast(MutableMapping[str, Any], templates.env.globals)["app_version"] = _app_version
cast(MutableMapping[str, Any], templates.env.globals)["source_label"] = source_label
cast(MutableMapping[str, Any], templates.env.globals)["source_is_broken"] = (
    source_is_broken
)
cast(MutableMapping[str, Any], templates.env.globals)["needs_confirm"] = needs_confirm
cast(MutableMapping[str, Any], templates.env.globals)["contract_label"] = contract_label
cast(MutableMapping[str, Any], templates.env.globals)["contract_choice_label"] = (
    contract_choice_label
)
cast(MutableMapping[str, Any], templates.env.globals)["choice_label"] = choice_label
