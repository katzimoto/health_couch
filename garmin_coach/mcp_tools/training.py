"""Planned-workout (training plan) MCP tools.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

from .runtime import db

__all__ = ["create_training_plan", "update_training_plan", "get_training_plan", "get_today_plan", "get_training_plans", "mark_plan_done", "mark_plan_skipped", "log_plan_feedback"]


def create_training_plan(
    day: str,
    title: str,
    goal: str | None = None,
    planned_start_time: str | None = None,
    estimated_duration_s: float | None = None,
    workout_type: str | None = None,
    exercises: list[dict] | None = None,
    cardio_plan: str | None = None,
    intensity_target: str | None = None,
    notes: str | None = None,
) -> dict:
    """Plan a workout for a specific date (YYYY-MM-DD). Later mark it via
    mark_plan_done / mark_plan_skipped so adherence can shape future
    recommendations. Returns the plan with its ``id``."""
    return db.create_training_plan(
        day, title=title, goal=goal, planned_start_time=planned_start_time,
        estimated_duration_s=estimated_duration_s, workout_type=workout_type,
        exercises=exercises, cardio_plan=cardio_plan,
        intensity_target=intensity_target, notes=notes,
    )
def update_training_plan(
    plan_id: int,
    day: str | None = None,
    title: str | None = None,
    goal: str | None = None,
    planned_start_time: str | None = None,
    estimated_duration_s: float | None = None,
    workout_type: str | None = None,
    exercises: list[dict] | None = None,
    cardio_plan: str | None = None,
    intensity_target: str | None = None,
    notes: str | None = None,
    status: str | None = None,
) -> dict:
    """Partially update an existing training plan — reduce sets, change the
    workout time, replace exercises, tighten intensity, or mark it adjusted
    for recovery, without recreating it. Only provided fields change; the
    rest of the plan is preserved. ``status`` (if given) must be one of
    planned|done|skipped|partially_done. Every changed field is recorded in
    the plan's edit history and ``updated_at`` is bumped. Returns the full
    updated plan, or an error if ``plan_id`` doesn't exist or ``status`` is
    invalid."""
    try:
        updated = db.update_training_plan(
            plan_id, day=day, title=title, goal=goal,
            planned_start_time=planned_start_time,
            estimated_duration_s=estimated_duration_s,
            workout_type=workout_type, exercises=exercises,
            cardio_plan=cardio_plan, intensity_target=intensity_target,
            notes=notes, status=status,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return updated or {"error": f"no training plan with id {plan_id}"}
def get_training_plan(plan_id: int) -> dict:
    """A single training plan by id, including its edit history (what
    changed and when)."""
    plan = db.get_training_plan(plan_id)
    if plan is None:
        return {"error": f"no training plan with id {plan_id}"}
    plan["history"] = db.training_plan_history(plan_id)
    return plan
def get_today_plan() -> list[dict]:
    """Today's planned workout(s) with status (planned / done / skipped /
    partially_done). Empty list if nothing is planned."""
    return db.get_today_training_plans()
def get_training_plans(days: int = 14, status: str | None = None) -> list[dict]:
    """Training plans over the last ``days`` (oldest first), optionally
    filtered by status — e.g. status='skipped' to see what keeps not
    happening."""
    return db.get_training_plans(days=max(1, min(days, 365)), status=status)
def mark_plan_done(
    plan_id: int,
    actual_duration_s: float | None = None,
    difficulty_rpe: float | None = None,
    notes: str | None = None,
    partially: bool = False,
) -> dict:
    """Mark a training plan completed (or ``partially=True`` for a partial
    session). Record actual duration and how hard it felt (RPE 1-10)."""
    updated = db.update_training_plan(
        plan_id,
        status="partially_done" if partially else "done",
        actual_duration_s=actual_duration_s,
        difficulty_rpe=difficulty_rpe,
        feedback=notes,
    )
    return updated or {"error": f"no training plan with id {plan_id}"}
def mark_plan_skipped(plan_id: int, reason: str | None = None) -> dict:
    """Mark a training plan skipped, with the reason (tired, sick, no time…)
    — the reason matters for tomorrow's recommendation."""
    updated = db.update_training_plan(plan_id, status="skipped", skip_reason=reason)
    return updated or {"error": f"no training plan with id {plan_id}"}
def log_plan_feedback(plan_id: int, feedback: str) -> dict:
    """Attach free-text feedback to a training plan (how it went, what to
    change next time)."""
    updated = db.update_training_plan(plan_id, feedback=feedback)
    return updated or {"error": f"no training plan with id {plan_id}"}


TOOLS = [create_training_plan, update_training_plan, get_training_plan, get_today_plan, get_training_plans, mark_plan_done, mark_plan_skipped, log_plan_feedback]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
