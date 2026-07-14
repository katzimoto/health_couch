"""Read/write MCP tools over the Garmin-derived metrics and trends.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

from datetime import date

from .runtime import analyzer, db

__all__ = ["get_daily_summary", "get_sleep_trend", "get_training_load", "get_body_composition_trend", "get_flags", "get_full_report", "log_weight", "log_hydration", "log_vital", "get_vitals", "log_readiness", "get_readiness", "log_body_measurements", "get_body_measurement_trend", "get_hydration", "get_hydration_trend"]


def get_daily_summary(days: int = 14) -> list[dict]:
    """Recent daily health summaries (sleep, HRV, resting HR, stress, steps,
    weight, body fat, training load), oldest first. ``days`` caps the window."""
    return db.daily_summary(days=max(1, min(days, 365)))
def get_sleep_trend(days: int = 30) -> dict:
    """Sleep hours and sleep score over the last ``days``, with 7d vs 28d
    averages and a sleep-debt figure."""
    report = analyzer.report()
    return {
        "sleep_hours_series": db.metric_series("sleep_hours", days),
        "sleep_score_series": db.metric_series("sleep_score", days),
        "trend": report.get("trends", {}).get("sleep_hours"),
        "sleep_debt_7d": report.get("sleep_debt_7d"),
    }
def get_training_load(days: int = 28) -> dict:
    """EWMA-weighted acute (7d) vs chronic (28d) training load, their ratio,
    and recent workouts (duplicates excluded). Ratio >1.5 = spike, <0.8 =
    detraining. Each workout's ``load_source`` says whether its load is
    garmin-provided, estimated (documented heuristic), or manually entered.
    A merged workout (manual strength + Garmin activity) counts once, with the
    Garmin load when available; ``merged_workouts`` lists these with their
    field provenance."""
    return {
        "acute_chronic": analyzer.acute_chronic_ratio(),
        "recent_workouts": db.recent_workouts(days=days),
        "merged_workouts": db.merged_workout_summaries(days=days),
    }
def get_body_composition_trend(days: int = 60) -> dict:
    """Weight and body-fat series over ``days`` with 7d vs 28d trend deltas."""
    report = analyzer.report()
    trends = report.get("trends", {})
    return {
        "weight_series": db.metric_series("weight_kg", days),
        "body_fat_series": db.metric_series("body_fat", days),
        "weight_trend": trends.get("weight_kg"),
        "body_fat_trend": trends.get("body_fat"),
    }
def get_flags() -> dict:
    """Current recovery/health flags (e.g. HRV decline, resting-HR jump,
    sleep debt, training-load spike) computed from the latest data."""
    report = analyzer.report()
    return {
        "as_of": report.get("as_of"),
        "flags": report.get("flags", []),
        "available": report.get("available", False),
    }
def get_full_report() -> dict:
    """Complete analyzer report in one call: latest daily summary, 7d-vs-28d
    trends for every tracked metric (sleep, HRV, resting HR, steps, stress,
    weight, body fat), sleep debt, acute:chronic training load, and flags.
    ``merged_workouts`` describes any field-level merged sessions (manual
    exercise log + Garmin physiology) so they read as one session, not two."""
    report = analyzer.report()
    report["merged_workouts"] = db.merged_workout_summaries(days=28)
    return report
def log_weight(
    weight_kg: float, day: str | None = None, body_fat: float | None = None
) -> dict:
    """Log a weight reading (e.g. from Apple Health or a scale the user tells
    you about). ``day`` defaults to today. Updates the provided fields on that
    day's entry (a later Garmin sync may update them again); other body-comp
    fields already stored for the day are kept."""
    target_day = day or date.today().isoformat()
    db.upsert_weight(target_day, weight_kg=weight_kg, body_fat=body_fat)
    return {"logged": True, "day": target_day, "weight_kg": weight_kg, "body_fat": body_fat}
def log_hydration(intake_ml: int, day: str | None = None, goal_ml: int | None = None) -> dict:
    """Log water/fluid intake for a day (e.g. from Apple Health). ``day``
    defaults to today. Updates the provided fields on that day's entry (a
    later Garmin sync may update them again)."""
    target_day = day or date.today().isoformat()
    db.upsert_hydration(target_day, intake_ml=intake_ml, goal_ml=goal_ml)
    return {"logged": True, "day": target_day, "intake_ml": intake_ml}
def log_vital(
    metric: str, value: float, unit: str = "", day: str | None = None, note: str = ""
) -> dict:
    """Log any biometric reading not covered by a dedicated tool — e.g. blood
    pressure ("blood_pressure_systolic"/"_diastolic"), "blood_glucose",
    "oxygen_saturation", "respiratory_rate", "height_cm", or any other named
    Apple Health metric the user gives you. ``day`` defaults to today."""
    target_day = day or date.today().isoformat()
    db.add_vital(metric=metric, value=value, day=target_day, unit=unit or None, note=note or None)
    return {"logged": True, "day": target_day, "metric": metric, "value": value}
def get_vitals(metric: str | None = None, days: int = 30) -> list[dict]:
    """Vitals logged via log_vital over the last ``days``, oldest first.
    Pass ``metric`` to filter to one named metric, or omit for all."""
    return db.recent_vitals(metric=metric, days=max(1, min(days, 365)))
def log_readiness(
    day: str | None = None,
    energy_1_10: int | None = None,
    soreness_1_10: int | None = None,
    motivation_1_10: int | None = None,
    sleep_quality_1_10: int | None = None,
    stress_1_10: int | None = None,
    mood: str | None = None,
    pain_areas: str | None = None,
    notes: str | None = None,
) -> dict:
    """Log the subjective daily check-in (1-10 scales). One entry per day —
    re-logging updates the provided fields. Combine with HRV/sleep/load when
    deciding how hard today should be."""
    target_day = day or date.today().isoformat()
    db.upsert_readiness(
        target_day, energy_1_10=energy_1_10, soreness_1_10=soreness_1_10,
        motivation_1_10=motivation_1_10, sleep_quality_1_10=sleep_quality_1_10,
        stress_1_10=stress_1_10, mood=mood, pain_areas=pain_areas, notes=notes,
    )
    return {"logged": True, "day": target_day}
def get_readiness(days: int = 14) -> list[dict]:
    """Readiness check-ins over the last ``days``, oldest first."""
    return db.recent_readiness(days=max(1, min(days, 365)))
def log_body_measurements(
    day: str | None = None,
    waist_cm: float | None = None,
    chest_cm: float | None = None,
    neck_cm: float | None = None,
    arm_cm: float | None = None,
    thigh_cm: float | None = None,
    hip_cm: float | None = None,
    notes: str | None = None,
) -> dict:
    """Log tape measurements (any subset). One entry per day — re-logging
    updates the provided fields. Tracks recomposition better than weight."""
    target_day = day or date.today().isoformat()
    db.upsert_body_measurement(
        target_day, waist_cm=waist_cm, chest_cm=chest_cm, neck_cm=neck_cm,
        arm_cm=arm_cm, thigh_cm=thigh_cm, hip_cm=hip_cm, notes=notes,
    )
    return {"logged": True, "day": target_day}
def get_body_measurement_trend(days: int = 90) -> dict:
    """Measurement series over ``days`` plus latest-vs-previous deltas per
    site (waist, chest, neck, arm, thigh, hip)."""
    rows = db.recent_body_measurements(days=max(1, min(days, 730)))
    deltas: dict = {}
    for field in ("waist_cm", "chest_cm", "neck_cm", "arm_cm", "thigh_cm", "hip_cm"):
        series = [(r["day"], r[field]) for r in rows if r[field] is not None]
        if not series:
            continue
        latest_day, latest = series[-1]
        previous = series[-2][1] if len(series) > 1 else None
        deltas[field] = {
            "latest": latest,
            "latest_day": latest_day,
            "previous": previous,
            "delta": round(latest - previous, 1) if previous is not None else None,
        }
    return {"series": rows, "deltas": deltas}
def get_hydration(days: int = 14) -> list[dict]:
    """Daily hydration entries (intake, goal, percent of goal) over the last
    ``days``, oldest first. Days never logged are absent."""
    return db.recent_hydration(days=max(1, min(days, 365)))
def get_hydration_trend(days: int = 7) -> dict:
    """Hydration over the last ``days``: 7d-style average intake, average
    percent of goal, and which days have no entry at all."""
    days = max(1, min(days, 90))
    rows = db.recent_hydration(days=days)
    from datetime import timedelta

    logged = {r["day"]: r for r in rows}
    window = [
        (date.today() - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)
    ]
    intakes = [r["intake_ml"] for r in rows if r["intake_ml"] is not None]
    pcts = [r["percent_of_goal"] for r in rows if r["percent_of_goal"] is not None]
    return {
        "days": days,
        "average_intake_ml": round(sum(intakes) / len(intakes)) if intakes else None,
        "average_percent_of_goal": round(sum(pcts) / len(pcts), 1) if pcts else None,
        "days_logged": len(rows),
        "missed_days": [d for d in window if d not in logged],
        "entries": rows,
    }


TOOLS = [get_daily_summary, get_sleep_trend, get_training_load, get_body_composition_trend, get_flags, get_full_report, log_weight, log_hydration, log_vital, get_vitals, log_readiness, get_readiness, log_body_measurements, get_body_measurement_trend, get_hydration, get_hydration_trend]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
