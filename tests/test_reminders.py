"""Offline tests for the Telegram reminder system: recurrence math, CRUD +
dedupe, pause/resume, soft delete, dispatch bookkeeping, health events, and
the MCP tool layer. No network — the Telegram sender is monkeypatched."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import garmin_coach.mcp_server as mcp
from garmin_coach.mcp_tools import runtime
import garmin_coach.scheduler as scheduler_mod
from garmin_coach.database import Database
from garmin_coach.reminders import (
    PRESET_REMINDERS,
    Reminders,
    as_utc,
    compute_next_run,
)
from garmin_coach.scheduler import SchedulerService
from garmin_coach.telegram_bot import TelegramCoach


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "reminders.db"))


@pytest.fixture()
def reminders(db) -> Reminders:
    return Reminders(db)


def _utc(y, m, d, hh, mm) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# ── compute_next_run ────────────────────────────────────────────────────────────

# 2026-07-01 is a Wednesday. Asia/Jerusalem is UTC+3 in July (IDT).
_NOW = _utc(2026, 7, 1, 8, 0)  # 11:00 local


def test_daily_later_today() -> None:
    nxt = compute_next_run("13:00", "Asia/Jerusalem", "daily", now=_NOW)
    assert nxt == _utc(2026, 7, 1, 10, 0)  # 13:00 IDT == 10:00 UTC


def test_daily_time_already_passed_rolls_to_tomorrow() -> None:
    nxt = compute_next_run("09:00", "Asia/Jerusalem", "daily", now=_NOW)
    assert nxt == _utc(2026, 7, 2, 6, 0)


def test_once_future_and_past() -> None:
    assert compute_next_run(
        "13:00", "Asia/Jerusalem", "once", "2026-07-03", now=_NOW
    ) == _utc(2026, 7, 3, 10, 0)
    assert compute_next_run(
        "13:00", "Asia/Jerusalem", "once", "2026-06-30", now=_NOW
    ) is None


def test_weekdays_skips_weekend() -> None:
    friday_evening = _utc(2026, 7, 3, 18, 0)  # Friday 21:00 local
    nxt = compute_next_run("09:00", "Asia/Jerusalem", "weekdays", now=friday_evening)
    assert nxt == _utc(2026, 7, 6, 6, 0)  # Monday


def test_weekly_anchored_to_date_weekday() -> None:
    # Anchor 2026-06-29 is a Monday; from Wednesday the next Monday is 07-06.
    nxt = compute_next_run("09:00", "Asia/Jerusalem", "weekly", "2026-06-29", now=_NOW)
    assert nxt == _utc(2026, 7, 6, 6, 0)


def test_rrule_recurrence() -> None:
    nxt = compute_next_run(
        "09:00", "Asia/Jerusalem", "RRULE:FREQ=WEEKLY;BYDAY=MO,TH", now=_NOW
    )
    assert nxt == _utc(2026, 7, 2, 6, 0)  # Thursday comes before Monday


def test_utc_timezone_days_roll() -> None:
    nxt = compute_next_run("08:30", "UTC", "daily", now=_NOW)
    assert nxt == _utc(2026, 7, 1, 8, 30)


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(time_str="25:00", tz_name="UTC", recurrence="daily"), "HH:MM"),
        (dict(time_str="9am", tz_name="UTC", recurrence="daily"), "HH:MM"),
        (dict(time_str="09:00", tz_name="Mars/Olympus", recurrence="daily"), "timezone"),
        (dict(time_str="09:00", tz_name="UTC", recurrence="fortnightly"), "recurrence"),
        (dict(time_str="09:00", tz_name="UTC", recurrence="once"), "date"),
        (dict(time_str="09:00", tz_name="UTC", recurrence="once", date_str="soon"), "YYYY-MM-DD"),
        (dict(time_str="09:00", tz_name="UTC", recurrence="RRULE:FREQ=BOGUS"), "RRULE"),
    ],
)
def test_invalid_inputs_raise(kwargs, match) -> None:
    with pytest.raises(ValueError, match=match):
        compute_next_run(now=_NOW, **kwargs)


# ── CRUD + dedupe ───────────────────────────────────────────────────────────────

def _lunch(reminders: Reminders, **overrides) -> dict:
    params = dict(
        title="Lunch log",
        message="Log lunch with the coach. Send photo/description or say skipped.",
        time="13:00",
        recurrence="daily",
        tags=["health", "meal", "lunch"],
    )
    params.update(overrides)
    return reminders.create(**params)


def test_create_returns_id_and_next_run(reminders: Reminders) -> None:
    created = _lunch(reminders)
    assert created["id"] is not None
    assert created["deduplicated"] is False
    assert created["enabled"] is True
    assert created["next_run_at"] is not None
    assert created["tags"] == ["health", "meal", "lunch"]
    assert created["timezone"] == "Asia/Jerusalem"  # default


def test_exact_duplicate_is_not_created(reminders: Reminders) -> None:
    first = _lunch(reminders)
    second = _lunch(reminders)
    assert second["deduplicated"] is True
    assert second["id"] == first["id"]
    assert len(reminders.list()) == 1


def test_different_time_is_not_a_duplicate(reminders: Reminders) -> None:
    _lunch(reminders)
    other = _lunch(reminders, time="13:30")
    assert other["deduplicated"] is False
    assert len(reminders.list()) == 2


def test_list_filters(reminders: Reminders) -> None:
    lunch = _lunch(reminders)
    _lunch(reminders, title="Dinner log", time="20:00", tags=["health", "meal", "dinner"])
    reminders.set_enabled(lunch["id"], False)

    assert {r["title"] for r in reminders.list()} == {"Lunch log", "Dinner log"}
    assert [r["title"] for r in reminders.list(enabled_only=True)] == ["Dinner log"]
    assert [r["title"] for r in reminders.list(tag="lunch")] == ["Lunch log"]


def test_edit_updates_in_place_and_recomputes(reminders: Reminders) -> None:
    created = _lunch(reminders)
    updated = reminders.edit(created["id"], time="13:30")
    assert updated["id"] == created["id"]
    assert updated["time"] == "13:30"
    assert updated["message"] == created["message"]  # untouched field preserved
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]
    assert as_utc(updated["next_run_at"]) != as_utc(created["next_run_at"])
    assert len(reminders.list()) == 1  # edited, not duplicated


def test_edit_rejects_bad_values_without_changing_anything(reminders: Reminders) -> None:
    created = _lunch(reminders)
    with pytest.raises(ValueError):
        reminders.edit(created["id"], time="lunchtime")
    assert reminders.get(created["id"])["time"] == "13:00"


def test_edit_missing_returns_none(reminders: Reminders) -> None:
    assert reminders.edit(999, time="13:30") is None


def test_pause_and_resume(reminders: Reminders) -> None:
    created = _lunch(reminders)
    paused = reminders.set_enabled(created["id"], False)
    assert paused["enabled"] is False

    resumed = reminders.set_enabled(created["id"], True)
    assert resumed["enabled"] is True
    assert resumed["next_run_at"] is not None


def test_soft_delete_stops_firing_but_keeps_history(reminders: Reminders) -> None:
    created = _lunch(reminders)
    reminders.mark_sent(created["id"], telegram_message_id=42)
    assert reminders.delete(created["id"]) is True
    assert reminders.delete(created["id"]) is False  # already gone

    assert reminders.list() == []
    row = reminders.get(created["id"])
    assert row["deleted_at"] is not None
    assert row["next_run_at"] is None
    far_future = datetime.now(timezone.utc) + timedelta(days=365)
    assert reminders.due(far_future) == []
    # Historical delivery records survive the delete.
    history = reminders.deliveries(reminder_id=created["id"])
    assert [d["status"] for d in history] == ["sent"]
    # And a deleted reminder can't be edited back to life.
    assert reminders.edit(created["id"], time="14:00") is None


# ── Dispatch bookkeeping ────────────────────────────────────────────────────────

def test_due_and_mark_sent_advances_schedule(reminders: Reminders) -> None:
    created = _lunch(reminders)
    fire_at = as_utc(created["next_run_at"])
    assert reminders.due(fire_at - timedelta(minutes=1)) == []
    due = reminders.due(fire_at + timedelta(seconds=30))
    assert [r["id"] for r in due] == [created["id"]]

    reminders.mark_sent(created["id"], telegram_message_id=7, now=fire_at + timedelta(seconds=30))
    after = reminders.get(created["id"])
    assert after["last_sent_at"] is not None
    assert as_utc(after["next_run_at"]) == fire_at + timedelta(days=1)
    assert reminders.due(fire_at + timedelta(minutes=5)) == []


def test_once_reminder_disables_after_send(reminders: Reminders) -> None:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    created = reminders.create(
        title="Blood test", message="Fast from 22:00", time="08:00",
        recurrence="once", date=tomorrow,
    )
    reminders.mark_sent(created["id"], telegram_message_id=1)
    after = reminders.get(created["id"])
    assert after["enabled"] is False
    assert after["next_run_at"] is None


def test_mark_failed_keeps_next_run_for_retry(reminders: Reminders) -> None:
    created = _lunch(reminders)
    before = as_utc(reminders.get(created["id"])["next_run_at"])
    reminders.mark_failed(created["id"], "boom")
    after = reminders.get(created["id"])
    assert as_utc(after["next_run_at"]) == before
    assert reminders.deliveries(created["id"])[0]["status"] == "error"
    assert reminders.deliveries(created["id"])[0]["error"] == "boom"


def test_mark_missed_advances_without_sending(reminders: Reminders) -> None:
    created = _lunch(reminders)
    fire_at = as_utc(created["next_run_at"])
    reminders.mark_missed(created["id"], now=fire_at + timedelta(hours=2))
    after = reminders.get(created["id"])
    assert after["last_sent_at"] is None
    assert as_utc(after["next_run_at"]) > fire_at
    assert reminders.deliveries(created["id"])[0]["status"] == "missed"


def test_scheduler_dispatch_sends_due_reminders(db, monkeypatch) -> None:
    service = SchedulerService(db=db)
    created = service.reminders.create(
        title="Ping", message="Drink water", time="13:00", timezone="UTC",
    )
    fire_at = as_utc(service.reminders.get(created["id"])["next_run_at"])

    sent: list[str] = []
    monkeypatch.setattr(
        scheduler_mod, "send_telegram_message",
        lambda text: sent.append(text) or 123,
    )

    class _Clock:
        now = fire_at + timedelta(seconds=10)

        @classmethod
        def tick(cls):  # pragma: no cover - helper
            return cls.now

    monkeypatch.setattr(
        scheduler_mod, "datetime",
        type("dt", (), {"now": staticmethod(lambda tz=None: _Clock.now)}),
    )

    service.dispatch_due_reminders()
    assert sent == ["Drink water"]
    after = service.reminders.get(created["id"])
    assert after["last_sent_at"] is not None
    assert service.reminders.deliveries(created["id"])[0]["status"] == "sent"
    assert service.reminders.deliveries(created["id"])[0]["telegram_message_id"] == 123


def test_scheduler_dispatch_marks_stale_reminders_missed(db, monkeypatch) -> None:
    service = SchedulerService(db=db)
    created = service.reminders.create(
        title="Old", message="Should not send", time="13:00", timezone="UTC",
    )
    fire_at = as_utc(service.reminders.get(created["id"])["next_run_at"])
    stale_now = fire_at + Reminders.SEND_GRACE + timedelta(minutes=5)

    monkeypatch.setattr(
        scheduler_mod, "datetime",
        type("dt", (), {"now": staticmethod(lambda tz=None: stale_now)}),
    )
    monkeypatch.setattr(
        scheduler_mod, "send_telegram_message",
        lambda text: pytest.fail("stale reminder must not be sent"),
    )

    service.dispatch_due_reminders()
    assert service.reminders.deliveries(created["id"])[0]["status"] == "missed"


def test_scheduler_dispatch_records_send_failures(db, monkeypatch) -> None:
    service = SchedulerService(db=db)
    created = service.reminders.create(
        title="Flaky", message="msg", time="13:00", timezone="UTC",
    )
    fire_at = as_utc(service.reminders.get(created["id"])["next_run_at"])

    def _boom(text):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(scheduler_mod, "send_telegram_message", _boom)
    monkeypatch.setattr(
        scheduler_mod, "datetime",
        type("dt", (), {"now": staticmethod(lambda tz=None: fire_at + timedelta(seconds=5))}),
    )

    service.dispatch_due_reminders()
    delivery = service.reminders.deliveries(created["id"])[0]
    assert delivery["status"] == "error"
    assert "telegram down" in delivery["error"]
    # next_run_at untouched → the next poll retries.
    assert as_utc(service.reminders.get(created["id"])["next_run_at"]) == fire_at


# ── Presets ─────────────────────────────────────────────────────────────────────

def test_presets_install_once(reminders: Reminders) -> None:
    first = reminders.create_presets()
    assert len(first) == len(PRESET_REMINDERS) == 4
    assert {r["title"] for r in first} == {
        "Morning plan", "Lunch log", "Dinner log", "Evening report",
    }
    assert all(r["deduplicated"] is False for r in first)

    second = reminders.create_presets()
    assert all(r["deduplicated"] is True for r in second)
    assert len(reminders.list()) == 4


# ── Health events + hydration accumulation ──────────────────────────────────────

def test_health_events_roundtrip(db: Database) -> None:
    db.add_health_event("skipped_meal", {"meal": "lunch"})
    db.add_health_event("hydration", {"added_ml": 500, "total_ml": 500})

    events = db.recent_health_events(days=1)
    assert [e["kind"] for e in events] == ["skipped_meal", "hydration"]
    assert events[0]["payload"] == {"meal": "lunch"}
    assert events[0]["source"] == "telegram"

    only_meals = db.recent_health_events(days=1, kind="skipped_meal")
    assert len(only_meals) == 1
    assert db.health_events_for_day(date.today())[1]["payload"]["total_ml"] == 500


def test_hydration_intake_accumulates(db: Database) -> None:
    assert db.add_hydration_intake(500) == 500
    assert db.add_hydration_intake(250) == 750
    today = db.recent_hydration(days=1)
    assert today[0]["intake_ml"] == 750


# ── Telegram bot logging helpers ────────────────────────────────────────────────

@pytest.fixture()
def bot(db) -> TelegramCoach:
    return TelegramCoach(db)


def test_bot_water_logging(bot: TelegramCoach) -> None:
    total = bot._log_water(500)
    assert total == 500
    events = bot.db.recent_health_events(days=1, kind="hydration")
    assert events[0]["payload"] == {"added_ml": 500, "total_ml": 500}


def test_bot_meal_logging(bot: TelegramCoach) -> None:
    bot._log_meal("chicken salad", photo=False)
    meals = bot.db.meals_for_day(date.today())
    assert [m["name"] for m in meals] == ["chicken salad"]
    events = bot.db.recent_health_events(days=1, kind="meal")
    assert events[0]["payload"]["text"] == "chicken salad"


def test_bot_skipped_meal_logging(bot: TelegramCoach) -> None:
    bot._log_skipped_meal("lunch")
    events = bot.db.recent_health_events(days=1, kind="skipped_meal")
    assert events[0]["payload"] == {"meal": "lunch"}
    assert any("lunch" in f["note"] for f in bot.db.recent_feedback(days=1))


def test_bot_workout_done_marks_todays_plan(bot: TelegramCoach) -> None:
    plan = bot.db.create_training_plan(date.today(), title="Push day")
    reply = bot._log_workout_done("felt strong")
    assert "Push day" in reply
    assert bot.db.get_today_training_plans()[0]["status"] == "done"
    events = bot.db.recent_health_events(days=1, kind="workout_done")
    assert events[0]["payload"]["plan_id"] == plan["id"]


def test_bot_workout_done_without_plan(bot: TelegramCoach) -> None:
    reply = bot._log_workout_done("")
    assert "logged" in reply.lower()
    assert bot.db.recent_health_events(days=1, kind="workout_done")


# ── MCP tool layer ──────────────────────────────────────────────────────────────

@pytest.fixture()
def mcp_env(db, monkeypatch) -> Database:
    monkeypatch.setattr(runtime, "_db", db)
    monkeypatch.setattr(runtime, "_garmin", None)
    return db


def test_mcp_reminder_lifecycle(mcp_env) -> None:
    created = mcp.create_telegram_reminder(
        title="Lunch log", message="Log lunch.", time="13:00",
        tags=["meal"],
    )
    assert created["id"] is not None

    listed = mcp.list_telegram_reminders()
    assert [r["id"] for r in listed] == [created["id"]]

    edited = mcp.edit_telegram_reminder(created["id"], time="13:30")
    assert edited["time"] == "13:30"
    assert len(mcp.list_telegram_reminders()) == 1

    paused = mcp.pause_telegram_reminder(created["id"])
    assert paused["enabled"] is False
    resumed = mcp.resume_telegram_reminder(created["id"])
    assert resumed["enabled"] is True

    assert mcp.delete_telegram_reminder(created["id"]) == {
        "deleted": True, "reminder_id": created["id"],
    }
    assert mcp.list_telegram_reminders() == []


def test_mcp_create_validates_input(mcp_env) -> None:
    result = mcp.create_telegram_reminder(
        title="Bad", message="x", time="25:99",
    )
    assert "error" in result


def test_mcp_edit_unknown_id(mcp_env) -> None:
    assert "error" in mcp.edit_telegram_reminder(12345, time="10:00")


def test_mcp_send_now_records_delivery(mcp_env, monkeypatch) -> None:
    monkeypatch.setattr(runtime, "send_telegram_message", lambda text: 555)
    result = mcp.send_telegram_message_now("hello", tags=["nudge"])
    assert result == {"sent": True, "telegram_message_id": 555}
    delivery = mcp.get_reminder_deliveries()[0]
    assert delivery["reminder_id"] is None
    assert delivery["status"] == "sent"
    assert delivery["meta"] == {"tags": ["nudge"]}


def test_mcp_send_now_failure_is_recorded(mcp_env, monkeypatch) -> None:
    def _fail(text):
        raise RuntimeError("no chat id")

    monkeypatch.setattr(runtime, "send_telegram_message", _fail)
    result = mcp.send_telegram_message_now("hello")
    assert result["sent"] is False
    assert mcp.get_reminder_deliveries()[0]["status"] == "error"


def test_mcp_default_reminders_and_events(mcp_env) -> None:
    installed = mcp.create_default_health_reminders()
    assert len(installed) == 4
    again = mcp.create_default_health_reminders()
    assert all(r["deduplicated"] for r in again)

    mcp_env.add_health_event("hydration", {"added_ml": 300, "total_ml": 300})
    events = mcp.get_health_events(days=1)
    assert events[-1]["kind"] == "hydration"
    assert mcp.get_health_events(days=1, kind="meal") == []
