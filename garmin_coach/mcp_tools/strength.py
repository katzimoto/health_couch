"""Strength-session logging, history, and weight recommendations.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

from datetime import date

from ..progression import recommend_next_weight, recovery_caution
from .runtime import analyzer, db

__all__ = ["log_strength_session", "get_strength_sessions", "get_exercise_history", "update_strength_session", "recommend_next_weights", "delete_strength_session"]


def log_strength_session(
    exercises: list[dict],
    day: str | None = None,
    session_name: str | None = None,
    gym: str | None = None,
    duration_s: float | None = None,
    calories: int | None = None,
    avg_hr: int | None = None,
    max_hr: int | None = None,
    notes: str | None = None,
) -> dict:
    """Log a detailed strength workout. Each exercise dict may carry:
    exercise_name (required), machine, planned_sets, planned_reps (e.g.
    "8-10"), planned_weight_kg, sets, reps, weight_kg, rpe, rir, rest_s,
    status (completed|skipped|substituted), substitute_exercise, pain_note,
    notes — and/or per-set detail as ``actual_sets``:
    ``[{"reps": 10, "weight_kg": 80, "rpe": 7}, ...]`` (aggregates are
    derived automatically). Everything except the name is optional. The
    session is mirrored into workout history with an estimated training
    load. Returns the stored session with its ``id`` and exercise rows."""
    return db.add_strength_session(
        day or date.today().isoformat(),
        exercises=exercises,
        session_name=session_name,
        gym=gym,
        duration_s=duration_s,
        calories=calories,
        avg_hr=avg_hr,
        max_hr=max_hr,
        notes=notes,
    )
def get_strength_sessions(days: int = 30) -> list[dict]:
    """Strength sessions (with exercises) over the last ``days``, oldest
    first."""
    return db.recent_strength_sessions(days=max(1, min(days, 365)))
def get_exercise_history(exercise_name: str, days: int = 180, limit: int = 20) -> list[dict]:
    """Past performances of one exercise, newest first: date, sets, reps,
    weight, estimated volume, best set, RPE/RIR. Use to check progressive
    overload and pick today's working weight."""
    return db.exercise_history(exercise_name, days=max(1, min(days, 730)), limit=limit)
def update_strength_session(
    session_id: int,
    exercises: list[dict] | None = None,
    session_name: str | None = None,
    gym: str | None = None,
    duration_s: float | None = None,
    calories: int | None = None,
    avg_hr: int | None = None,
    max_hr: int | None = None,
    notes: str | None = None,
) -> dict:
    """Correct a strength session. Only provided fields change; passing
    ``exercises`` replaces the whole exercise list (same schema as
    log_strength_session, including ``actual_sets``). The mirrored workout
    row and its estimated load are refreshed."""
    updated = db.update_strength_session(
        session_id, exercises=exercises, session_name=session_name, gym=gym,
        duration_s=duration_s, calories=calories, avg_hr=avg_hr,
        max_hr=max_hr, notes=notes,
    )
    return updated or {"error": f"no strength session with id {session_id}"}
def recommend_next_weights(exercises: list[str] | None = None, days: int = 120) -> dict:
    """Per-exercise prescription for the next session: recommended weight,
    increase/maintain/reduce action, and the reasoning — from the last
    logged performance (RPE-based double progression, 2.5 kg plate steps)
    gated by current recovery (HRV/sleep/load flags + fresh readiness
    check-in). Omit ``exercises`` to cover everything trained recently. Use
    this to write a workout with exact weights instead of guessing."""
    report = analyzer.report()
    caution = recovery_caution(report)
    names = exercises if exercises is not None else db.recently_trained_exercises(days=days)
    recommendations = []
    for name in names:
        history = db.exercise_history(name, days=days, limit=1)
        if not history:
            recommendations.append(
                {"exercise": name, "action": "log_first",
                 "reason": "no logged history in the window"}
            )
            continue
        rec = recommend_next_weight(history[0], caution, exercise_name=name)
        rec["exercise"] = name
        rec["last_trained"] = history[0]["date"]
        recommendations.append(rec)
    return {"recovery_caution": caution, "recommendations": recommendations}
def delete_strength_session(session_id: int) -> dict:
    """Delete a strength session, its exercises, and its mirrored workout
    row (daily summaries recompute automatically)."""
    deleted = db.delete_strength_session(session_id)
    return {"deleted": deleted, "session_id": session_id}


TOOLS = [log_strength_session, get_strength_sessions, get_exercise_history, update_strength_session, recommend_next_weights, delete_strength_session]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
