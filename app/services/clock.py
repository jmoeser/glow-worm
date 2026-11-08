"""Brisbane (or configured TIMEZONE) clock used by scheduled money movement."""

from datetime import date, datetime

from app.config import TIMEZONE


def now() -> datetime:
    """Timezone-aware current time. ``datetime.now(TIMEZONE)`` is not LMT."""
    return datetime.now(TIMEZONE)


def today() -> date:
    return now().date()


def as_configured(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return TIMEZONE.localize(moment)
    return moment.astimezone(TIMEZONE)


def month_window_open(moment: datetime | None = None) -> bool:
    """True at and after 00:05 on the 1st, through the rest of that month.

    The threshold must be ``TIMEZONE.localize(...)``. Passing ``tzinfo=TIMEZONE``
    uses the zone's first offset (Australia/Brisbane LMT, +10:12), which is
    twelve minutes earlier than 00:05.
    """
    current = as_configured(now() if moment is None else moment)
    threshold = TIMEZONE.localize(datetime(current.year, current.month, 1, 0, 5))
    return current >= threshold
