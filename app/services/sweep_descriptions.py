"""Exact ledger descriptions for monthly income and overflow sweeps.

The repair script and the month bootstrap both use these strings. A hyphen
or a locale-specific month name is not a match.
"""

import calendar

_EM = "\u2014"
SURPLUS_PREFIX = f"Budget surplus sweep {_EM} "
SHORTFALL_PREFIX = f"Budget shortfall {_EM} "
INCOME_PREFIX = f"Monthly income {_EM} "
CORRECTION_PREFIX = f"Budget correction {_EM} "


def month_label(month: int, year: int) -> str:
    return f"{calendar.month_name[month]} {year}"


def income_description(month: int, year: int) -> str:
    """System income row for one processed month."""
    return f"{INCOME_PREFIX}{month_label(month, year)}"


def sweep_descriptions(month: int, year: int) -> tuple[str, str]:
    """Surplus-sweep and shortfall descriptions for one month, as posted."""
    label = month_label(month, year)
    return (f"{SURPLUS_PREFIX}{label}", f"{SHORTFALL_PREFIX}{label}")


def correction_description(month: int, year: int) -> str:
    return f"{CORRECTION_PREFIX}{month_label(month, year)}"


def _parse_label(
    description: str, prefixes: tuple[str, ...]
) -> tuple[str, int, int] | None:
    label = None
    matched = None
    for prefix in prefixes:
        if description.startswith(prefix):
            label = description[len(prefix) :]
            matched = prefix
            break
    if label is None or matched is None:
        return None
    name, sep, year_text = label.partition(" ")
    if sep == "" or name not in calendar.month_name:
        return None
    month = list(calendar.month_name).index(name)
    if month == 0 or not year_text.isdigit():
        return None
    year = int(year_text)
    if label != month_label(month, year):
        return None
    return matched, month, year


def parse_sweep(description: str | None) -> tuple[int, int] | None:
    """Return ``(month, year)`` when ``description`` is a sweep or shortfall."""
    parsed = parse_sweep_kind(description)
    if parsed is None:
        return None
    _kind, month, year = parsed
    return month, year


def parse_sweep_kind(description: str | None) -> tuple[str, int, int] | None:
    """Return ``(surplus|shortfall, month, year)`` for an exact sweep description."""
    if not description:
        return None
    parsed = _parse_label(description, (SURPLUS_PREFIX, SHORTFALL_PREFIX))
    if parsed is None:
        return None
    prefix, month, year = parsed
    kind = "surplus" if prefix == SURPLUS_PREFIX else "shortfall"
    return kind, month, year


def parse_correction(description: str | None) -> tuple[int, int] | None:
    """Return ``(month, year)`` for an exact budget-spent repair description."""
    if not description:
        return None
    parsed = _parse_label(description, (CORRECTION_PREFIX,))
    if parsed is None:
        return None
    _prefix, month, year = parsed
    return month, year


def parse_income_description(description: str | None) -> tuple[int, int] | None:
    """Return ``(month, year)`` for the exact system income description."""
    if not description:
        return None
    parsed = _parse_label(description, (INCOME_PREFIX,))
    if parsed is None:
        return None
    _prefix, month, year = parsed
    return month, year
