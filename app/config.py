"""Application configuration derived from environment variables."""

import logging
import os
import re

import pytz
from dotenv import load_dotenv

# Idempotent. Needed so `from app.tasks import ...` one-liners see .env
# even when app.database has not been imported yet.
load_dotenv()

logger = logging.getLogger(__name__)

TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Australia/Brisbane"))

_DAY_RE = re.compile(r"[a-z0-9,\-]+")


def _int_env(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    if value < lo or value > hi:
        logger.warning("%s=%s is outside %s-%s; using %s", name, value, lo, hi, default)
        return default
    return value


def _float_env(name: str, default: float, *, lo: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    if value < lo:
        logger.warning("%s=%s is below %s; using %s", name, value, lo, default)
        return default
    return value


def _parse_hhmm(raw: str | None, default: tuple[int, int] = (18, 0)) -> tuple[int, int]:
    if raw is None or raw.strip() == "":
        return default
    parts = raw.strip().split(":")
    if len(parts) != 2:
        logger.warning(
            "Invalid SHARES_UPDATE_TIME %r; using %02d:%02d",
            raw,
            default[0],
            default[1],
        )
        return default
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        logger.warning(
            "Invalid SHARES_UPDATE_TIME %r; using %02d:%02d",
            raw,
            default[0],
            default[1],
        )
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        logger.warning(
            "Invalid SHARES_UPDATE_TIME %r; using %02d:%02d",
            raw,
            default[0],
            default[1],
        )
        return default
    return hour, minute


def _load_timezone(name: str, default: str) -> pytz.BaseTzInfo:
    raw = os.getenv(name, default).strip() or default
    try:
        return pytz.timezone(raw)
    except pytz.UnknownTimeZoneError:
        logger.warning("Unknown timezone %r for %s; using %s", raw, name, default)
        return pytz.timezone(default)


def _load_days(name: str, default: str) -> str:
    raw = os.getenv(name, default).strip().lower() or default
    if _DAY_RE.fullmatch(raw) is None:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    return raw


ALPHA_VANTAGE_API_KEY: str | None = (
    os.getenv("ALPHA_VANTAGE_API_KEY", "").strip() or None
)

SHARES_UPDATE_TIMEZONE = _load_timezone("SHARES_UPDATE_TIMEZONE", "America/New_York")
SHARES_UPDATE_HOUR, SHARES_UPDATE_MINUTE = _parse_hhmm(os.getenv("SHARES_UPDATE_TIME"))
SHARES_UPDATE_DAYS = _load_days("SHARES_UPDATE_DAYS", "mon-fri")
SHARES_AV_MAX_REQUESTS_PER_RUN = _int_env(
    "SHARES_AV_MAX_REQUESTS_PER_RUN", 20, lo=1, hi=25
)
SHARES_AV_MIN_INTERVAL_SECONDS = _float_env(
    "SHARES_AV_MIN_INTERVAL_SECONDS", 13.0, lo=12.0
)
SHARES_PRICE_UPSERT_DAYS = _int_env("SHARES_PRICE_UPSERT_DAYS", 10, lo=1, hi=100)
SHARES_CATCHUP_ON_STARTUP = (
    os.getenv("SHARES_CATCHUP_ON_STARTUP", "true").strip().lower() == "true"
)
SHARES_FRANKFURTER_BASE_URL = (
    os.getenv("SHARES_FRANKFURTER_BASE_URL", "https://api.frankfurter.dev").strip()
    or "https://api.frankfurter.dev"
)
