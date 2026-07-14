"""Manual workout logging plus dedupe/merge MCP tools.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

from datetime import date

from garmin_coach.storage.database import synthetic_activity_id as _synthetic_activity_id
from garmin_coach.domain.training_load import estimate_training_load
from .runtime import db

__all__ = ["log_workout", "update_workout", "delete_workout", "get_duplicate_workouts", "dedupe_workouts", "set_activity_source_priority", "merge_garmin_strength_fragments", "merge_workout_sources", "get_merged_workout", "unmerge_workout_sources", "backfill_workout_source_merges"]


def log_workout(
    name: str,
    type: str,
    day: str | None = None,
    duration_s: float | None = None,
    distance_m: float | None = None,
    calories: int | None = None,
    avg_hr: int | None = None,
    max_hr: int | None = None,
    training_load: float | None = None,
) -> dict:
    """Log a workout not synced from Garmin (e.g. from Apple Health or a
    manual description). ``day`` defaults to today. Assigned a synthetic
    negative activity ID so it never collides with a real Garmin activity.
    Pass ``training_load`` to override; otherwise it's estimated from
    type/duration/HR so load analysis doesn't read the workout as rest."""
    target_day = day or date.today().isoformat()
    activity_id = _synthetic_activity_id()
    load_source = "manual" if training_load is not None else None
    if training_load is None:
        training_load = estimate_training_load(type, duration_s, avg_hr=avg_hr)
        load_source = "estimated" if training_load is not None else None
    db.upsert_workout(
        activity_id,
        target_day,
        name=name,
        type=type,
        duration_s=duration_s,
        distance_m=distance_m,
        calories=calories,
        avg_hr=avg_hr,
        max_hr=max_hr,
        training_load=training_load,
        source="manual",
        load_source=load_source,
    )
    return {
        "logged": True,
        "day": target_day,
        "activity_id": activity_id,
        "training_load": training_load,
        "load_source": load_source,
    }
def update_workout(
    activity_id: int,
    name: str | None = None,
    type: str | None = None,
    duration_s: float | None = None,
    distance_m: float | None = None,
    calories: int | None = None,
    avg_hr: int | None = None,
    max_hr: int | None = None,
    training_load: float | None = None,
) -> dict:
    """Correct a workout. Only provided fields change. Setting
    ``training_load`` marks it manually entered."""
    fields: dict = dict(
        name=name, type=type, duration_s=duration_s, distance_m=distance_m,
        calories=calories, avg_hr=avg_hr, max_hr=max_hr,
        training_load=training_load,
    )
    if training_load is not None:
        fields["load_source"] = "manual"
    updated = db.update_workout(activity_id, **fields)
    if updated is None:
        return {"error": f"no workout with activity_id {activity_id}"}
    return {"updated": True, "day": updated["day"], "workouts": db.workouts_for_day(updated["day"])}
def delete_workout(activity_id: int) -> dict:
    """Delete a workout (e.g. a wrong or duplicated entry). Returns the
    day's remaining workouts; daily summaries recompute automatically."""
    day = db.delete_workout(activity_id)
    if day is None:
        return {"error": f"no workout with activity_id {activity_id}"}
    return {"deleted": True, "day": day, "workouts": db.workouts_for_day(day)}
def get_duplicate_workouts(days: int = 60) -> list[list[dict]]:
    """Groups of same-day activities that look like one workout recorded by
    multiple sources (similar duration/distance/calories). Detection only —
    review before calling dedupe_workouts."""
    return db.find_duplicate_workouts(days=max(1, min(days, 365)))
def dedupe_workouts(days: int = 60) -> dict:
    """Soft-delete duplicates: in each group the highest-priority source
    (default garmin > apple > manual; see set_activity_source_priority) is
    kept and the rest are marked ``duplicate_of`` the keeper. Marked rows
    disappear from summaries and training load but stay in the database
    (no hard delete). Returns what was marked."""
    return db.dedupe_workouts(days=max(1, min(days, 365)))
def set_activity_source_priority(priority: list[str]) -> dict:
    """Which source wins when the same workout exists twice, highest first —
    e.g. ["garmin", "apple", "manual"]."""
    profile = db.set_profile(activity_source_priority=",".join(priority))
    return {"activity_source_priority": profile["activity_source_priority"]}
def merge_garmin_strength_fragments(
    day: str,
    dry_run: bool = False,
    min_fragments: int = 2,
    max_gap_minutes: float = 90.0,
) -> dict:
    """Merge same-day Garmin strength-training fragments (the watch
    sometimes records one gym session as several short activities) into one
    canonical workout, so training load and recovery flags aren't inflated
    by double-counting. Only ``source="garmin"`` activities of a
    strength-like type are considered; manual logs and already-merged rows
    are untouched. Set ``dry_run=true`` to preview the effect without
    changing data. Originals are never deleted — they're marked
    ``duplicate_of`` the merged row (same mechanism as dedupe_workouts), so
    they drop out of summaries/training load/workout counts but stay in the
    database. Idempotent: re-running for the same day updates the existing
    merged row(s) instead of creating new ones. If two separate qualifying
    sessions happened the same day, the largest is reported at the top
    level and any others under ``other_merges``."""
    return db.merge_garmin_strength_fragments(
        day, dry_run=dry_run,
        min_fragments=max(1, min_fragments),
        max_gap_minutes=max(1.0, max_gap_minutes),
    )
def merge_workout_sources(
    day: str | None = None,
    activity_id: int | None = None,
    source_activity_ids: list[int] | None = None,
    force: bool = False,
    dry_run: bool = False,
    min_confidence: float = 0.5,
) -> dict:
    """Link a manual strength log and a Garmin activity of the *same* session
    into one canonical workout, combining them field-by-field instead of
    hiding one: manual keeps the exercise details (names/order/sets/reps/
    weights/RPE/notes), Garmin supplies physiology (HR, calories, duration,
    training load, timing). Both source rows are preserved and marked
    duplicate of the canonical, so summaries and training load count the
    session once. Returns which fields came from which source.

    Pass ``day`` (or ``activity_id``, whose day is used) to auto-match strength
    sessions on that day; pass ``source_activity_ids`` to link specific rows
    yourself (to override a bad auto-match). ``force`` accepts low-confidence
    matches; ``dry_run`` previews without writing."""
    return db.merge_workout_sources(
        day=day, activity_id=activity_id, source_activity_ids=source_activity_ids,
        force=force, dry_run=dry_run, min_confidence=min_confidence,
    )
def get_merged_workout(activity_id: int) -> dict:
    """Everything about one workout: the canonical summary, per-field
    provenance (which source gave exercise details vs HR vs calories vs
    duration vs training load), each linked source record with what it
    contributed, the strength exercises, and the Garmin physiology fields.
    Works on any workout — an unmerged one simply has no linked sources."""
    result = db.get_merged_workout(activity_id)
    return result if result is not None else {"error": f"no workout with activity_id {activity_id}"}
def unmerge_workout_sources(canonical_activity_id: int) -> dict:
    """Undo a field-level merge (override a wrong match): restore each source
    row into its own workout, reattach the strength session to its manual row,
    and delete the canonical. No data is lost — source rows were never
    deleted. Re-link differently with merge_workout_sources(source_activity_ids=...)."""
    return db.unmerge_workout_sources(canonical_activity_id)
def backfill_workout_source_merges(days: int = 3650, min_confidence: float = 0.6) -> dict:
    """One-off: link historical manual strength sessions to same-day Garmin
    strength activities where confidence is high. Idempotent and non-
    destructive (source rows are kept). Use ``min_confidence`` to tune how
    strict the historical matching is."""
    return db.backfill_workout_source_merges(
        days=max(1, min(days, 36500)), min_confidence=min_confidence
    )


TOOLS = [log_workout, update_workout, delete_workout, get_duplicate_workouts, dedupe_workouts, set_activity_source_priority, merge_garmin_strength_fragments, merge_workout_sources, get_merged_workout, unmerge_workout_sources, backfill_workout_source_merges]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
