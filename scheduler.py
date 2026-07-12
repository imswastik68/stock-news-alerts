"""
Long-running service: runs the pipeline on a fixed interval and, optionally,
sends a daily summary. Sends a startup message on boot so you know the bot is
alive without waiting for the first alert.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.alerting.telegram_bot import send_daily_summary, send_startup_message
from src.config import configure_logging, get_settings
from src.pipeline import run_pipeline
from src.storage.db import get_session, get_todays_stats

logger = logging.getLogger(__name__)


def _run_pipeline_job() -> None:
    try:
        run_pipeline()
    except Exception as exc:
        logger.error("scheduler: pipeline run crashed: %s", exc)


def _daily_summary_job() -> None:
    session = get_session()
    try:
        stats = get_todays_stats(session)
    finally:
        session.close()
    try:
        send_daily_summary(stats["processed"], stats["alerts_sent"])
    except Exception as exc:
        logger.error("scheduler: daily summary send crashed: %s", exc)


def main() -> None:
    configure_logging()
    settings = get_settings()

    scheduler = BlockingScheduler(timezone="Asia/Kolkata")
    scheduler.add_job(
        _run_pipeline_job,
        trigger=IntervalTrigger(minutes=settings.poll_interval_minutes),
        max_instances=1,
        coalesce=True,
        id="pipeline",
    )

    if settings.daily_summary_enabled:
        scheduler.add_job(
            _daily_summary_job,
            trigger=CronTrigger(hour=settings.daily_summary_hour, minute=0, timezone="Asia/Kolkata"),
            id="daily_summary",
        )

    try:
        send_startup_message()
    except Exception as exc:
        logger.error("scheduler: startup message failed: %s", exc)

    logger.info(
        "scheduler: starting — polling every %d min, daily summary %s",
        settings.poll_interval_minutes,
        f"at {settings.daily_summary_hour}:00 IST" if settings.daily_summary_enabled else "disabled",
    )

    # Kick off an immediate first run rather than waiting a full interval.
    _run_pipeline_job()

    try:
        scheduler.start()
    except KeyboardInterrupt:
        logger.info("scheduler: shutting down")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
