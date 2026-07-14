"""Meal logging, nutrition summaries and gaps, and bulk Apple Health import.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..database import synthetic_activity_id as _synthetic_activity_id
from ..nutrition_gaps import (
    DEFAULT_TIMEZONE as NUTRITION_TIMEZONE,
    categorize_meals,
    missing_meals as _missing_meals,
    recommend_next_meal,
    sugar_status,
    workout_meal_flags,
)
from ..training_load import estimate_training_load
from .runtime import db

__all__ = ["log_meal", "get_meals", "log_apple_health_export", "get_nutrition_summary", "get_nutrition_gaps", "update_meal", "delete_meal"]


def log_meal(
    name: str,
    calories: int | None = None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    fiber_g: float | None = None,
    sugar_g: float | None = None,
    sodium_mg: float | None = None,
    day: str | None = None,
    source: str | None = None,
    source_record_id: str | None = None,
    is_estimated: bool | None = None,
    note: str = "",
) -> dict:
    """Log a meal (e.g. from a description, photo, or Apple Health nutrition
    entry the user gives you). All macros are optional — a calories-only meal is
    valid and totals only sum what's present. ``day`` defaults to today (ISO
    ``YYYY-MM-DD``). Pass ``source`` + ``source_record_id`` for idempotent
    imports (re-logging the same source record updates it in place). Returns the
    day's meals so far."""
    target_day = day or date.today().isoformat()
    db.add_meal(
        name=name,
        day=target_day,
        calories=calories,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        fiber_g=fiber_g,
        sugar_g=sugar_g,
        sodium_mg=sodium_mg,
        source=source,
        source_record_id=source_record_id,
        is_estimated=is_estimated,
        note=note or None,
    )
    return {"logged": True, "day": target_day, "meals_today": db.recent_meals(days=1)}
def get_meals(days: int = 7) -> list[dict]:
    """Meals logged over the last ``days``, oldest first."""
    return db.recent_meals(days=max(1, min(days, 365)))
def log_apple_health_export(records: list[dict]) -> dict:
    """Bulk-import structured records read from an Apple Health export (or
    any other source) in one call, instead of calling the single-item log_*
    tools in a loop. Read the export yourself (the user will share it, e.g.
    as a file), extract the values, and pass every record here at once. Each
    record is a dict shaped like one of:

        {"kind": "weight", "day": "2026-07-01", "weight_kg": 78.2, "body_fat": 18.5}
        {"kind": "meal", "day": "2026-07-01", "name": "Breakfast", "calories": 350,
         "protein_g": 20, "carbs_g": 40, "fat_g": 10, "fiber_g": 5, "sugar_g": 8}
        {"kind": "workout", "day": "2026-07-01", "name": "Run", "type": "running",
         "duration_s": 1800, "distance_m": 5000, "calories": 320, "avg_hr": 145}
        {"kind": "hydration", "day": "2026-07-01", "intake_ml": 500}
        {"kind": "vital", "day": "2026-07-01", "metric": "blood_pressure_systolic",
         "value": 120, "unit": "mmHg"}

    ``day`` defaults to today if omitted. Every record is applied
    independently — one bad or malformed record doesn't fail the batch — and
    per-record status is returned so failures are visible.
    """
    results: list[dict] = []
    for i, rec in enumerate(records):
        kind = rec.get("kind")
        day = rec.get("day") or date.today().isoformat()
        try:
            if kind == "weight":
                db.upsert_weight(day, weight_kg=rec["weight_kg"], body_fat=rec.get("body_fat"))
            elif kind == "meal":
                db.add_meal(
                    name=rec["name"],
                    day=day,
                    calories=rec.get("calories"),
                    protein_g=rec.get("protein_g"),
                    carbs_g=rec.get("carbs_g"),
                    fat_g=rec.get("fat_g"),
                    fiber_g=rec.get("fiber_g"),
                    sugar_g=rec.get("sugar_g"),
                    sodium_mg=rec.get("sodium_mg"),
                    source=rec.get("source"),
                    source_record_id=rec.get("source_record_id"),
                    is_estimated=rec.get("is_estimated"),
                    note=rec.get("note"),
                )
            elif kind == "workout":
                est = estimate_training_load(
                    rec.get("type"), rec.get("duration_s"), avg_hr=rec.get("avg_hr")
                )
                db.upsert_workout(
                    _synthetic_activity_id(),
                    day,
                    name=rec.get("name"),
                    type=rec.get("type"),
                    duration_s=rec.get("duration_s"),
                    distance_m=rec.get("distance_m"),
                    calories=rec.get("calories"),
                    avg_hr=rec.get("avg_hr"),
                    max_hr=rec.get("max_hr"),
                    training_load=est,
                    source="apple",
                    load_source="estimated" if est is not None else None,
                )
            elif kind == "hydration":
                db.upsert_hydration(day, intake_ml=rec["intake_ml"], goal_ml=rec.get("goal_ml"))
            elif kind == "vital":
                db.add_vital(
                    metric=rec["metric"],
                    value=rec["value"],
                    day=day,
                    unit=rec.get("unit"),
                    note=rec.get("note"),
                )
            else:
                results.append({"index": i, "status": f"error: unknown kind {kind!r}"})
                continue
            results.append({"index": i, "status": "ok"})
        except Exception as exc:  # noqa: BLE001 — one bad record must not abort the batch
            results.append({"index": i, "status": f"error: {exc}"})
    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {
        "total": len(records),
        "ok": ok_count,
        "failed": len(records) - ok_count,
        "results": results,
    }
def get_nutrition_summary(days: int = 7, day: str | None = None) -> list[dict]:
    """Per-day nutrition totals (calories + macros), meal count, the meals
    themselves, and — when a profile sets targets — calories/protein
    remaining. Pass ``day`` for a single date. Use for 'what should I eat
    next today?'. Meals logged without macros contribute only what they
    carry."""
    return db.nutrition_summary(days=max(1, min(days, 90)), day=day)
def get_nutrition_gaps(day: str | None = None) -> dict:
    """What's still needed today: remaining calories/macros vs profile
    targets, which of breakfast/lunch/dinner haven't been logged (only once
    their time window has started, and never counted as "missing" if the
    user explicitly logged a skipped-meal event instead), whether a
    pre/post-workout meal is missing around today's planned workout, and a
    concrete suggestion for what to eat next. ``day`` defaults to today in
    Asia/Jerusalem. Unset profile targets show up as ``null``, not zero."""
    tz = ZoneInfo(NUTRITION_TIMEZONE)
    now_local = datetime.now(tz)
    today_str = now_local.date().isoformat()
    target_day = day or today_str

    # nutrition_summary already carries calorie/protein targets and their
    # remaining amounts (it reads the same profile) — reuse those instead of
    # re-deriving them; only carbs/fat/fiber need a fresh profile read here.
    summary = db.nutrition_summary(day=target_day)[0]
    profile = db.get_profile() or {}
    targets = {
        "calories": summary["calorie_target"],
        "protein_g": summary["protein_target_g"],
        "carbs_g": profile.get("carbs_target_g"),
        "fat_g": profile.get("fat_target_g"),
        "fiber_g": profile.get("fiber_target_g"),
    }
    consumed = {
        "calories": summary["total_calories"],
        "protein_g": summary["total_protein_g"],
        "carbs_g": summary["total_carbs_g"],
        "fat_g": summary["total_fat_g"],
        "fiber_g": summary["total_fiber_g"],
    }

    def remaining(key: str) -> float | None:
        target = targets[key]
        if target is None:
            return None
        return round(target - (consumed[key] or 0), 1)

    remaining_ = {
        "calories": summary["calories_remaining"],
        "protein_g": summary["protein_remaining_g"],
        "carbs_g": remaining("carbs_g"),
        "fat_g": remaining("fat_g"),
        "fiber_g": remaining("fiber_g"),
    }

    events = db.health_events_for_day(target_day)
    skipped = {
        (e.get("payload") or {}).get("meal") for e in events if e["kind"] == "skipped_meal"
    }
    skipped.discard(None)
    meals = db.meals_for_day(target_day)
    logged = categorize_meals(meals)

    if target_day < today_str:
        effective_now = now_local.replace(hour=23, minute=59)  # a past day — all windows elapsed
    elif target_day > today_str:
        effective_now = now_local.replace(hour=0, minute=0)  # a future day — nothing due yet
    else:
        effective_now = now_local
    missing = _missing_meals(logged, skipped, effective_now)

    workout_time = None
    estimated_duration_s = None
    if target_day == today_str:
        plans_today = [p for p in db.get_training_plans_for_day(target_day) if p["status"] == "planned"]
        if plans_today:
            workout_time = plans_today[0].get("planned_start_time")
            estimated_duration_s = plans_today[0].get("estimated_duration_s")
    flags = workout_meal_flags(workout_time, estimated_duration_s, meals, now_local, NUTRITION_TIMEZONE)

    alerts: list[str] = []
    if remaining_["protein_g"] is not None and remaining_["protein_g"] > 20:
        alerts.append("Protein is behind target.")
    if remaining_["fiber_g"] is not None and remaining_["fiber_g"] > 10:
        alerts.append("Fiber is low.")
    for name in missing:
        alerts.append(f"{name.capitalize()} not logged.")
    if flags["pre_workout_meal_missing"]:
        alerts.append("Pre-workout meal not logged.")
    if flags["post_workout_meal_missing"]:
        alerts.append("Post-workout meal not logged.")

    return {
        "day": target_day,
        "targets": targets,
        "consumed": {**consumed, "sugar_g": summary["total_sugar_g"]},
        "remaining": remaining_,
        "sugar_status": sugar_status(summary["total_sugar_g"]),
        "meal_count": summary["meal_count"],
        "missing_meals": missing,
        "pre_workout_meal_missing": flags["pre_workout_meal_missing"],
        "post_workout_meal_missing": flags["post_workout_meal_missing"],
        "alerts": alerts,
        "recommended_next_meal": recommend_next_meal(remaining_),
    }
def update_meal(
    meal_id: int,
    name: str | None = None,
    calories: int | None = None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    fiber_g: float | None = None,
    sugar_g: float | None = None,
    note: str | None = None,
) -> dict:
    """Correct a logged meal (estimates are often wrong). Only provided
    fields change. ``meal_id`` comes from get_meals. Returns that day's
    meals after the update."""
    day = db.update_meal(
        meal_id, name=name, calories=calories, protein_g=protein_g,
        carbs_g=carbs_g, fat_g=fat_g, fiber_g=fiber_g, sugar_g=sugar_g,
        note=note,
    )
    if day is None:
        return {"error": f"no meal with id {meal_id}"}
    return {"updated": True, "day": day, "meals": db.meals_for_day(day)}
def delete_meal(meal_id: int) -> dict:
    """Delete a logged meal. Returns the day's remaining meals."""
    day = db.delete_meal(meal_id)
    if day is None:
        return {"error": f"no meal with id {meal_id}"}
    return {"deleted": True, "day": day, "meals": db.meals_for_day(day)}


TOOLS = [log_meal, get_meals, log_apple_health_export, get_nutrition_summary, get_nutrition_gaps, update_meal, delete_meal]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
