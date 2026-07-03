"""Scheduler service — the automation heartbeat.

On startup it ensures the database exists and, if it's empty, runs the initial
backfill. Then it schedules two recurring jobs with APScheduler:

* a daily Garmin pull (early morning, before the plan) that refreshes yesterday
  and today, and
* the 07:30 morning-plan push to Telegram.

Runs as its own container command (``python -m garmin_coach.scheduler``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .database import Database
from .garmin_client import GarminClient
from .telegram_bot import TelegramCoach

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("garmin_coach.scheduler")


class SchedulerService:
    def __init__(self) -> None:
        self.db = Database()
        self.garmin = GarminClient(self.db)
        self.telegram = TelegramCoach(self.db)

    def daily_pull(self) -> None:
        """Refresh yesterday and today (today may still be partial)."""
        log.info("Running daily Garmin pull.")
        try:
            self.garmin.pull_day(date.today() - timedelta(days=1))
            self.garmin.pull_day(date.today())
        except Exception:  # noqa: BLE001
            log.exception("Daily pull failed.")

    def initial_backfill_if_empty(self) -> None:
        """Backfill history the first time the DB has no summary rows."""
        if self.db.daily_summary(days=1):
            log.info("Database already populated — skipping backfill.")
            return
        log.info("Empty database — running %d-day backfill.", settings.backfill_days)
        try:
            self.garmin.backfill()
        except Exception:  # noqa: BLE001
            log.exception("Backfill failed (will retry on next daily pull).")

    async def morning_plan_job(self) -> None:
        log.info("Running morning-plan job.")
        try:
            await self.telegram.push_morning_plan()
        except Exception:  # noqa: BLE001
            log.exception("Morning-plan push failed.")

    async def run(self) -> None:
        # Ensure Garmin auth is usable up front so failures are obvious at boot.
        try:
            self.garmin.login()
        except Exception:  # noqa: BLE001
            log.exception(
                "Garmin login failed — run scripts/garmin_login.py to refresh "
                "tokens. Continuing; jobs will retry."
            )
        self.initial_backfill_if_empty()

        hour, minute = settings.morning_plan_hm()
        scheduler = AsyncIOScheduler(timezone=settings.timezone)

        # Daily pull an hour before the plan so the plan sees fresh data.
        pull_hour = (hour - 1) % 24
        scheduler.add_job(
            self.daily_pull,
            CronTrigger(hour=pull_hour, minute=minute, timezone=settings.timezone),
            id="daily_pull",
            replace_existing=True,
        )
        scheduler.add_job(
            self.morning_plan_job,
            CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
            id="morning_plan",
            replace_existing=True,
        )
        scheduler.start()
        log.info(
            "Scheduler up. Daily pull at %02d:%02d, morning plan at %02d:%02d (%s).",
            pull_hour, minute, hour, minute, settings.timezone,
        )

        # Run one pull now so a fresh deploy has current data without waiting.
        self.daily_pull()

        # Keep the event loop alive.
        stop = asyncio.Event()
        await stop.wait()


def main() -> None:
    asyncio.run(SchedulerService().run())


if __name__ == "__main__":
    main()
