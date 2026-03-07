"""Application configuration derived from environment variables."""

import os

import pytz

TIMEZONE = pytz.timezone(os.getenv("TIMEZONE", "Australia/Brisbane"))
