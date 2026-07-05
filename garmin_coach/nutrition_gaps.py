"""Heuristics behind ``get_nutrition_gaps``: which meals look missing today
and whether pre/post-workout meals happened, without ever assuming an
unlogged meal was skipped — only an explicit skipped-meal event counts as
"skipped". Kept as pure functions over already-fetched rows so they're
unit-testable without a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as tz_utc
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Jerusalem"

# Local-hour windows used only to bucket *logged* meals into a category for
# the missing-meal check — never to guess what a meal's contents are.
MEAL_WINDOWS: tuple[tuple[str, int, int], ...] = (
    ("breakfast", 4, 11),
    ("lunch", 11, 16),
    ("dinner", 16, 24),
)


def _to_local(ts: Any, tz: ZoneInfo) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=tz_utc.utc)  # stored as UTC, like everywhere else here
    return ts.astimezone(tz)


def categorize_meals(meals: list[dict[str, Any]], tz_name: str = DEFAULT_TIMEZONE) -> set[str]:
    """Which of breakfast/lunch/dinner have at least one logged meal, judged
    by the local time-of-day the meal was logged (not its name/text)."""
    tz = ZoneInfo(tz_name)
    found: set[str] = set()
    for meal in meals:
        local = _to_local(meal.get("ts"), tz)
        if local is None:
            continue
        for name, start, end in MEAL_WINDOWS:
            if start <= local.hour < end:
                found.add(name)
                break
    return found


def missing_meals(
    logged: set[str], skipped: set[str], now_local: datetime
) -> list[str]:
    """Meal categories whose window has already started and that are
    neither logged nor explicitly marked skipped (via a skipped_meal event)."""
    return [
        name for name, start, _end in MEAL_WINDOWS
        if now_local.hour >= start and name not in logged and name not in skipped
    ]


def workout_meal_flags(
    workout_time: str | None,
    estimated_duration_s: float | None,
    meals: list[dict[str, Any]],
    now_local: datetime,
    tz_name: str = DEFAULT_TIMEZONE,
) -> dict[str, bool | None]:
    """Pre/post-workout meal presence for today's planned workout.

    Returns ``None`` for a flag until it's actually due to be checked (e.g.
    the pre-workout window hasn't started yet), so a plan for later today
    doesn't get flagged as missing its meals prematurely.
    """
    if not workout_time:
        return {"pre_workout_meal_missing": None, "post_workout_meal_missing": None}
    try:
        hour, minute = (int(p) for p in workout_time.split(":"))
    except ValueError:
        return {"pre_workout_meal_missing": None, "post_workout_meal_missing": None}

    tz = ZoneInfo(tz_name)
    workout_dt = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end_dt = workout_dt + timedelta(seconds=estimated_duration_s or 3600)

    def has_meal_between(start: datetime, end: datetime) -> bool:
        for meal in meals:
            local = _to_local(meal.get("ts"), tz)
            if local is not None and start <= local <= end:
                return True
        return False

    pre_missing = None
    if now_local >= workout_dt - timedelta(minutes=30):
        pre_missing = not has_meal_between(workout_dt - timedelta(hours=3), workout_dt)

    post_missing = None
    if now_local >= end_dt + timedelta(minutes=45):
        post_missing = not has_meal_between(end_dt, end_dt + timedelta(hours=3))

    return {"pre_workout_meal_missing": pre_missing, "post_workout_meal_missing": post_missing}


def sugar_status(total_sugar_g: float | None) -> str:
    if total_sugar_g is None:
        return "unknown"
    if total_sugar_g > 50:
        return "high"
    if total_sugar_g > 25:
        return "moderate"
    return "low"


def recommend_next_meal(remaining: dict[str, float | None]) -> dict[str, Any]:
    """Coaching-flavor suggestion for what to eat next, from what's left."""
    calories_left = remaining.get("calories")
    if calories_left is not None and calories_left <= 0:
        return {
            "style": "light, mostly vegetables and lean protein — you're at/over "
                     "your calorie target",
            "protein_g": "15-25",
            "carbs_g": "0-20",
            "examples": [
                "grilled chicken and salad", "cottage cheese and cucumber",
                "egg whites and greens",
            ],
        }
    protein_left = remaining.get("protein_g")
    carbs_left = remaining.get("carbs_g")
    protein_bucket = "40-50" if (protein_left is None or protein_left >= 40) else "20-30"
    carbs_bucket = "60-100" if (carbs_left is None or carbs_left >= 40) else "20-40"
    return {
        "style": "high protein + carbs + vegetables",
        "protein_g": protein_bucket,
        "carbs_g": carbs_bucket,
        "examples": [
            "chicken with rice and salad",
            "tuna, eggs, bread, and vegetables",
            "cottage cheese, oats, banana, and nuts",
        ],
    }
