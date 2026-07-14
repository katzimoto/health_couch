"""Profile/goals, sleep target, and the feature-request backlog tools.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

from .runtime import db

__all__ = ["get_profile", "set_profile", "set_sleep_target", "create_feature_request", "list_feature_requests", "update_feature_request"]


def get_profile() -> dict | None:
    """The user's profile and goals (age, sex, height, target weight, goal
    type, training level, injuries, equipment, food restrictions, calorie/
    macro targets). Read this before recommending training or food. Returns
    null if no profile has been set yet."""
    return db.get_profile()
def set_profile(
    age: int | None = None,
    sex: str | None = None,
    height_cm: float | None = None,
    current_weight_kg: float | None = None,
    target_weight_kg: float | None = None,
    goal_type: str | None = None,
    training_level: str | None = None,
    injuries_or_limitations: str | None = None,
    available_equipment: str | None = None,
    preferred_training_days: str | None = None,
    food_restrictions: str | None = None,
    calorie_target: int | None = None,
    protein_target_g: float | None = None,
    carbs_target_g: float | None = None,
    fat_target_g: float | None = None,
    fiber_target_g: float | None = None,
    sleep_target_hours: float | None = None,
    sleep_preferred_min_hours: float | None = None,
    sleep_preferred_max_hours: float | None = None,
    sleep_minimum_recovery_hours: float | None = None,
    hydration_baseline_target_ml: int | None = None,
    hydration_training_day_target_ml: int | None = None,
    hydration_hot_day_target_ml: int | None = None,
    hydration_medical_limit_note: str | None = None,
    notes: str | None = None,
    replace: bool = False,
) -> dict:
    """Create or update the user profile. Partial by default — only the
    fields you pass change. Set ``replace=True`` only when the user
    explicitly wants the whole profile rewritten. goal_type: fat_loss |
    muscle_gain | recomposition | endurance | general_health; training_level:
    beginner | intermediate | advanced.

    Sleep target defaults to a 7.0h baseline (never a hard-coded 8h). To change
    the sleep target with a proper effective date (so historical sleep-debt
    stays reproducible), prefer ``set_sleep_target``; setting
    ``sleep_target_hours`` here updates the current value only. Hydration
    targets persist here and drive the coaching recommendation."""
    return db.set_profile(
        replace=replace,
        age=age, sex=sex, height_cm=height_cm,
        current_weight_kg=current_weight_kg, target_weight_kg=target_weight_kg,
        goal_type=goal_type, training_level=training_level,
        injuries_or_limitations=injuries_or_limitations,
        available_equipment=available_equipment,
        preferred_training_days=preferred_training_days,
        food_restrictions=food_restrictions, calorie_target=calorie_target,
        protein_target_g=protein_target_g, carbs_target_g=carbs_target_g,
        fat_target_g=fat_target_g, fiber_target_g=fiber_target_g,
        sleep_target_hours=sleep_target_hours,
        sleep_preferred_min_hours=sleep_preferred_min_hours,
        sleep_preferred_max_hours=sleep_preferred_max_hours,
        sleep_minimum_recovery_hours=sleep_minimum_recovery_hours,
        hydration_baseline_target_ml=hydration_baseline_target_ml,
        hydration_training_day_target_ml=hydration_training_day_target_ml,
        hydration_hot_day_target_ml=hydration_hot_day_target_ml,
        hydration_medical_limit_note=hydration_medical_limit_note,
        notes=notes,
    )
def set_sleep_target(
    target_hours: float,
    effective_from: str | None = None,
    minimum_recovery_hours: float | None = None,
    preferred_min_hours: float | None = None,
    preferred_max_hours: float | None = None,
    note: str | None = None,
) -> dict:
    """Set the user's sleep-need target (hours) with an effective date so past
    sleep-debt numbers stay reproducible. ``effective_from`` defaults to today.
    The baseline is 7.0h — this replaces any hard-coded 8h assumption. Sleep
    debt is computed as the shortfall vs this target (an estimate, not a
    physiological measurement)."""
    return db.set_sleep_target(
        target_hours=target_hours,
        effective_from=effective_from,
        minimum_recovery_hours=minimum_recovery_hours,
        preferred_min_hours=preferred_min_hours,
        preferred_max_hours=preferred_max_hours,
        note=note,
    )
def create_feature_request(
    title: str,
    description: str | None = None,
    priority: str | None = None,
    related_endpoint: str | None = None,
    requested_by: str | None = None,
) -> dict:
    """Record a feature request in the server-side backlog (a real table, not a
    free-text profile note). status starts as ``requested``. priority: low |
    medium | high."""
    return db.create_feature_request(
        title=title, description=description, priority=priority,
        related_endpoint=related_endpoint, requested_by=requested_by,
    )
def list_feature_requests(status: str | None = None) -> list[dict]:
    """List backlog feature requests, newest first. Filter by ``status``:
    requested | planned | in_progress | blocked | implemented | rejected."""
    return db.list_feature_requests(status=status)
def update_feature_request(
    request_id: int,
    status: str | None = None,
    priority: str | None = None,
    resolution_notes: str | None = None,
    description: str | None = None,
) -> dict:
    """Update a feature request (status/priority/notes). Returns the updated row,
    or an error if the id is unknown."""
    result = db.update_feature_request(
        request_id, status=status, priority=priority,
        resolution_notes=resolution_notes, description=description,
    )
    return result or {"error": f"No feature request with id {request_id}"}


TOOLS = [get_profile, set_profile, set_sleep_target, create_feature_request, list_feature_requests, update_feature_request]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
