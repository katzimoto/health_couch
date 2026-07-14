"""Reminder-time math for ``create_workout_reminder_pack``.

Pure functions over (date, time, duration) — no database access — so the
scheduling logic is unit-testable on its own. Offsets are picked from within
the ranges the coach mission specifies, anchored so a single planned workout
produces a natural-reading sequence of nudges around it:

    pre-workout meal  ── 120 min before start (within the 90-150 min window)
    hydration         ──  45 min before start (within the 30-45 min window)
    gym / workout start ── at the planned start time
    post-workout meal ──  45 min after the expected end (within 45-90 min)
    workout log       ──  60 min after the expected end
"""

from __future__ import annotations

from datetime import date as date_type, datetime, timedelta

from garmin_coach.domain.reminders import TIME_RE

DEFAULT_TIMEZONE = "Asia/Jerusalem"
DEFAULT_DURATION_S = 3600.0  # assumed when a plan has no estimated_duration_s

PRE_MEAL_BEFORE_MIN = 120
HYDRATION_BEFORE_MIN = 45
POST_MEAL_AFTER_END_MIN = 45
WORKOUT_LOG_AFTER_END_MIN = 60

REMINDER_TYPES = (
    "pre_workout_meal", "hydration", "gym_start", "post_workout_meal", "workout_log",
)


def compute_reminder_pack_times(
    workout_date: str,
    workout_time: str,
    estimated_duration_s: float | None = None,
) -> dict[str, tuple[str, str]]:
    """Local (date, "HH:MM") for each reminder type around one workout.

    Raises ``ValueError`` with a clear message for a malformed date or time —
    callers should surface that directly rather than guess a default.
    """
    match = TIME_RE.match((workout_time or "").strip())
    if not match:
        raise ValueError(f"workout_time must be HH:MM (24h), got {workout_time!r}")
    hour, minute = int(match[1]), int(match[2])
    try:
        day = date_type.fromisoformat((workout_date or "").strip())
    except ValueError as exc:
        raise ValueError(f"date must be YYYY-MM-DD, got {workout_date!r}") from exc

    start = datetime(day.year, day.month, day.day, hour, minute)
    duration_s = (
        estimated_duration_s if estimated_duration_s and estimated_duration_s > 0
        else DEFAULT_DURATION_S
    )
    end = start + timedelta(seconds=duration_s)

    def at(dt: datetime) -> tuple[str, str]:
        return dt.date().isoformat(), dt.strftime("%H:%M")

    return {
        "pre_workout_meal": at(start - timedelta(minutes=PRE_MEAL_BEFORE_MIN)),
        "hydration": at(start - timedelta(minutes=HYDRATION_BEFORE_MIN)),
        "gym_start": at(start),
        "post_workout_meal": at(end + timedelta(minutes=POST_MEAL_AFTER_END_MIN)),
        "workout_log": at(end + timedelta(minutes=WORKOUT_LOG_AFTER_END_MIN)),
    }
