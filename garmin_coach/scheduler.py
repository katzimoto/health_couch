"""Scheduler service — the automation heartbeat.

On startup it ensures the database exists and, if it's empty, runs the initial
backfill. Then it schedules the recurring jobs with APScheduler:

* an hourly Garmin sync of *today*, so the coach, /status and the dashboard
  track the day as it happens instead of yesterday's snapshot,
* a daily Garmin pull (early morning, before the plan) that refreshes yesterday
  and today, then heals a few days of any backfill gap,
* the 07:30 morning-plan push to Telegram (retried with backoff on failure),
* a nightly SQLite backup with rotation, and
* a liveness heartbeat for the container healthcheck.

Runs as its own container command (``python -m garmin_coach.scheduler``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .database import Database
from .garmin_client import GarminClient
from .heartbeat import beat
from .reminders import Reminders, as_utc
from .telegram_bot import TelegramCoach
from .telegram_sender import send_telegram_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("garmin_coach.scheduler")

# The morning plan is the headline feature — one transient OpenAI/Telegram
# error must not skip the day. Waits before attempts 2 and 3.
_PLAN_RETRY_DELAYS_S = (120, 300)

_BACKUP_KEEP = 7
_HEARTBEAT_INTERVAL_MIN = 5


class SchedulerService:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()
        self.garmin = GarminClient(self.db)
        self.telegram = TelegramCoach(self.db)
        self.reminders = Reminders(self.db)

    def daily_pull(self) -> None:
        """Refresh yesterday and today, then heal a slice of any history gap."""
        log.info("Running daily Garmin pull.")
        try:
            self.garmin.pull_day(date.today() - timedelta(days=1))
            self.garmin.pull_day(date.today())
        except Exception:  # noqa: BLE001
            log.exception("Daily pull failed.")
        try:
            self.garmin.pull_missing_days()
        except Exception:  # noqa: BLE001
            log.exception("Gap healing failed (will retry tomorrow).")
        beat("scheduler")

    def hourly_pull(self) -> None:
        """Refresh today's metrics so the day is tracked as it happens.

        Today only (~9 data-endpoint calls) — yesterday's finalisation and
        gap healing stay with the daily pull, keeping the added Garmin API
        pressure modest.
        """
        log.info("Running hourly Garmin sync.")
        try:
            self.garmin.pull_day(date.today())
        except Exception:  # noqa: BLE001
            log.exception("Hourly sync failed (next run in an hour).")
        beat("scheduler")

    def initial_backfill_if_empty(self) -> None:
        """Backfill history the first time the DB has no summary rows.

        Only the *completely empty* case runs here; a backfill that died
        partway leaves data behind, and those holes are healed incrementally
        by ``pull_missing_days`` in the daily pull.
        """
        if self.db.has_data():
            log.info("Database already populated — skipping backfill.")
            return
        log.info("Empty database — running %d-day backfill.", settings.backfill_days)
        try:
            self.garmin.backfill()
        except Exception:  # noqa: BLE001
            log.exception(
                "Backfill failed — remaining days will be healed by the "
                "nightly gap check."
            )

    async def morning_plan_job(self) -> None:
        log.info("Running morning-plan job.")
        attempts = 1 + len(_PLAN_RETRY_DELAYS_S)
        for attempt in range(1, attempts + 1):
            try:
                await self.telegram.push_morning_plan()
                return
            except Exception:  # noqa: BLE001
                log.exception(
                    "Morning-plan push failed (attempt %d/%d).", attempt, attempts
                )
                if attempt < attempts:
                    await asyncio.sleep(_PLAN_RETRY_DELAYS_S[attempt - 1])
        log.error("Morning plan not delivered after %d attempts.", attempts)

    async def reminders_job(self) -> None:
        """Poll for due Telegram reminders once a minute and deliver them.

        Runs the blocking work (SQLite + Telegram HTTP) in a worker thread so
        the event loop's other jobs never stall behind a slow send.
        """
        try:
            await asyncio.to_thread(self.dispatch_due_reminders)
        except Exception:  # noqa: BLE001 — one bad tick must not kill the job
            log.exception("Reminder dispatch failed (next poll in a minute).")

    def dispatch_due_reminders(self) -> None:
        """Send every due reminder; failure policy per reminder:

        * within the grace window a failed send keeps ``next_run_at`` in
          place, so the next minute's poll retries;
        * past the grace window (persistent failure, or the container was
          down) the firing is recorded as *missed* and the schedule advances
          — no flood of stale reminders on recovery.

        Every outcome lands in telegram_reminder_delivery, so failures are
        visible in the data as well as the logs.
        """
        now = datetime.now(timezone.utc)
        for reminder in self.reminders.due(now):
            overdue = now - as_utc(reminder["next_run_at"])
            if overdue > Reminders.SEND_GRACE:
                log.warning(
                    "Reminder %s (%r) missed its window by %s — skipping to "
                    "the next occurrence.",
                    reminder["id"], reminder["title"], overdue,
                )
                self.reminders.mark_missed(reminder["id"], now=now)
                continue
            try:
                message_id = send_telegram_message(reminder["message"])
            except Exception as exc:  # noqa: BLE001 — record + retry next poll
                log.exception(
                    "Reminder %s (%r) failed to send — will retry for %s.",
                    reminder["id"], reminder["title"],
                    Reminders.SEND_GRACE - overdue,
                )
                self.reminders.mark_failed(reminder["id"], str(exc))
                continue
            self.reminders.mark_sent(reminder["id"], message_id, now=now)
            log.info("Sent reminder %s (%r).", reminder["id"], reminder["title"])

    def backup_db(self) -> None:
        """Nightly online backup of the SQLite file, keeping the newest few."""
        backups = Path(settings.db_path).expanduser().parent / "backups"
        dest = backups / f"health-{date.today().isoformat()}.db"
        try:
            self.db.backup_to(dest)
            for old in sorted(backups.glob("health-*.db"))[:-_BACKUP_KEEP]:
                old.unlink()
            log.info("Backed up database to %s.", dest)
        except Exception:  # noqa: BLE001
            log.exception("Database backup failed.")

    def build_scheduler(self) -> AsyncIOScheduler:
        """Register every recurring job (separate from run() so tests can
        assert the schedule without starting an event loop)."""
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
        # Hourly sync of today, offset 30 min from the daily pull/plan minute
        # so the two never fire together.
        hourly_minute = (minute + 30) % 60
        scheduler.add_job(
            self.hourly_pull,
            CronTrigger(minute=hourly_minute, timezone=settings.timezone),
            id="hourly_pull",
            replace_existing=True,
            # If a pull overruns the hour, skip the pile-up, not the schedule.
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            self.morning_plan_job,
            CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
            id="morning_plan",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            self.backup_db,
            CronTrigger(hour=3, minute=0, timezone=settings.timezone),
            id="db_backup",
            replace_existing=True,
        )
        # Reminder delivery needs minute resolution — reminders fire at
        # arbitrary HH:MM in arbitrary timezones, so a fixed cron can't cover
        # them; a cheap due-row query runs every minute instead.
        scheduler.add_job(
            self.reminders_job,
            "interval",
            minutes=1,
            id="telegram_reminders",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        scheduler.add_job(
            lambda: beat("scheduler"),
            "interval",
            minutes=_HEARTBEAT_INTERVAL_MIN,
            id="heartbeat",
            replace_existing=True,
        )
        return scheduler

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

        scheduler = self.build_scheduler()
        scheduler.start()
        beat("scheduler")
        hour, minute = settings.morning_plan_hm()
        log.info(
            "Scheduler up. Hourly sync at :%02d, daily pull at %02d:%02d, "
            "morning plan at %02d:%02d, backup at 03:00 (%s).",
            (minute + 30) % 60, (hour - 1) % 24, minute, hour, minute,
            settings.timezone,
        )

        # Run one pull now so a fresh deploy has current data without waiting
        # (in a worker thread — scheduled jobs must not wait behind it).
        await asyncio.to_thread(self.daily_pull)

        # Keep the event loop alive.
        stop = asyncio.Event()
        await stop.wait()


def main() -> None:
    asyncio.run(SchedulerService().run())


if __name__ == "__main__":
    main()
