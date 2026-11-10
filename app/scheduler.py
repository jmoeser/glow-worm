"""APScheduler configuration for background tasks."""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import app.config as app_config
from app.config import TIMEZONE
from app.services.household_months import recover_current_month
from app.tasks import (
    clear_market_data_stop,
    process_due_bills,
    process_income_allocation,
    process_share_market_data,
    process_share_market_data_if_stale,
    request_market_data_stop,
)

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def register_jobs(target=scheduler) -> None:
    """Add the recurring jobs. Split out so tests can inspect them."""
    # Income allocation: 1st of each month at 00:05 in TIMEZONE
    target.add_job(
        process_income_allocation,
        CronTrigger(day=1, hour=0, minute=5, timezone=TIMEZONE),
        id="income_allocation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )
    # Current month only. A missed 1st is not replayed from the in-memory store.
    run_at = datetime.now(TIMEZONE) + timedelta(seconds=60)
    target.add_job(
        recover_current_month,
        DateTrigger(run_date=run_at),
        id="income_allocation_catchup",
        replace_existing=True,
        max_instances=1,
    )
    target.add_job(
        recover_current_month,
        CronTrigger(minute=0, timezone=TIMEZONE),
        id="income_allocation_retry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )

    # Bill processing: daily at 06:00 in TIMEZONE.
    # misfire_grace_time=None runs a late fire once. It does not replay
    # days the process was down. The in-memory store has forgotten them.
    target.add_job(
        process_due_bills,
        CronTrigger(hour=6, minute=0, timezone=TIMEZONE),
        id="bill_processing",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
    )
    # Three minutes, not 60 seconds. Income recovery is already at 60 seconds
    # and assigns the whole Bills balance. This one-shot is not gated by
    # SHARES_CATCHUP_ON_STARTUP. A boot that posts income can still overwrite
    # a Bills balance; this only separates the two moments.
    bill_catchup_at = datetime.now(TIMEZONE) + timedelta(minutes=3)
    target.add_job(
        process_due_bills,
        DateTrigger(run_date=bill_catchup_at),
        id="bill_processing_catchup",
        replace_existing=True,
        max_instances=1,
    )

    # Share prices and FX: after the US close, on US trading days.
    target.add_job(
        process_share_market_data,
        CronTrigger(
            day_of_week=app_config.SHARES_UPDATE_DAYS,
            hour=app_config.SHARES_UPDATE_HOUR,
            minute=app_config.SHARES_UPDATE_MINUTE,
            timezone=app_config.SHARES_UPDATE_TIMEZONE,
        ),
        id="share_market_data",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )

    if app_config.SHARES_CATCHUP_ON_STARTUP:
        run_at = datetime.now(app_config.SHARES_UPDATE_TIMEZONE) + timedelta(seconds=60)
        target.add_job(
            process_share_market_data_if_stale,
            DateTrigger(run_date=run_at),
            id="share_market_data_catchup",
            replace_existing=True,
            max_instances=1,
        )


def start_scheduler() -> None:
    """Configure and start the background scheduler."""
    clear_market_data_stop()
    register_jobs(scheduler)
    scheduler.start()
    logger.info(
        "Background scheduler started with income_allocation, "
        "bill_processing, and share_market_data jobs"
    )


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    request_market_data_stop()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Background scheduler stopped")
