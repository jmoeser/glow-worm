"""APScheduler configuration for background tasks."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.tasks import process_due_bills, process_income_allocation

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def start_scheduler() -> None:
    """Configure and start the background scheduler."""
    # Income allocation: 1st of each month at 00:05 AEST
    scheduler.add_job(
        process_income_allocation,
        CronTrigger(day=1, hour=0, minute=5, timezone="Australia/Brisbane"),
        id="income_allocation",
        replace_existing=True,
    )

    # Bill processing: daily at 06:00 AEST
    scheduler.add_job(
        process_due_bills,
        CronTrigger(hour=6, minute=0, timezone="Australia/Brisbane"),
        id="bill_processing",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Background scheduler started with income_allocation and bill_processing jobs")


def stop_scheduler() -> None:
    """Shut down the scheduler gracefully."""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Background scheduler stopped")
