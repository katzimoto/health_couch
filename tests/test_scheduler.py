"""Offline tests for the scheduler's job registration (no event loop, no network)."""

from __future__ import annotations

import pytest

from garmin_coach.database import Database
from garmin_coach.scheduler import SchedulerService


@pytest.fixture()
def service(tmp_path) -> SchedulerService:
    return SchedulerService(db=Database(path=str(tmp_path / "sched.db")))


def test_all_recurring_jobs_registered(service: SchedulerService) -> None:
    jobs = {job.id: job for job in service.build_scheduler().get_jobs()}
    assert set(jobs) == {
        "hourly_pull", "daily_pull", "morning_plan", "db_backup", "heartbeat",
    }


def test_hourly_sync_fires_every_hour_offset_from_daily(service: SchedulerService) -> None:
    jobs = {job.id: job for job in service.build_scheduler().get_jobs()}

    def field(job, name):
        return str(next(f for f in job.trigger.fields if f.name == name))

    # Every hour...
    assert field(jobs["hourly_pull"], "hour") == "*"
    # ...at a different minute than the daily pull, so they never collide.
    assert field(jobs["hourly_pull"], "minute") != field(jobs["daily_pull"], "minute")
    # And the daily pull stays a once-a-day job.
    assert field(jobs["daily_pull"], "hour") != "*"
