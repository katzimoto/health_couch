"""Telegram reminders, workout reminder packs, and the guided log flow.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

import logging

from garmin_coach.domain.reminders import DEFAULT_TIMEZONE
from garmin_coach.domain.workout_reminders import REMINDER_TYPES, compute_reminder_pack_times
from . import runtime
from .runtime import db, reminders, workout_log_flows

log = logging.getLogger("garmin_coach.mcp")

__all__ = ["create_workout_reminder_pack", "create_telegram_reminder", "list_telegram_reminders", "edit_telegram_reminder", "pause_telegram_reminder", "resume_telegram_reminder", "delete_telegram_reminder", "send_telegram_message_now", "create_default_health_reminders", "get_reminder_deliveries", "get_health_events", "start_workout_log_flow"]


_REMINDER_PACK_TAG = "workout_reminder_pack"
_REMINDER_PACK_COPY: dict[str, tuple[str, str]] = {
    "pre_workout_meal": (
        "Pre-workout meal",
        "Eat a pre-workout meal before {title} at {time} on {date}.",
    ),
    "hydration": (
        "Hydration",
        "Hydrate before {title} at {time} on {date} — water/electrolytes now.",
    ),
    "gym_start": ("Workout time", "Time to start: {title} ({date})."),
    "post_workout_meal": (
        "Post-workout meal",
        "Eat your post-workout meal — protein + carbs to kick off recovery ({date}).",
    ),
    "workout_log": (
        "Log your workout",
        "Log how {title} went (sets/reps/RPE) so tomorrow's plan can adjust ({date}).",
    ),
}
def _linked_reminders_by_type(plan_id: int) -> dict[str, dict]:
    """Non-deleted reminders previously created by this tool for ``plan_id``,
    keyed by reminder type — one list scan instead of one per type."""
    out: dict[str, dict] = {}
    for r in reminders.list():
        meta = r.get("metadata") or {}
        if meta.get("plan_id") == plan_id and meta.get("reminder_type"):
            out[meta["reminder_type"]] = r
    return out
def create_workout_reminder_pack(
    plan_id: int,
    workout_time: str | None = None,
    timezone: str = DEFAULT_TIMEZONE,
    date: str | None = None,
    include_pre_workout_meal: bool = True,
    include_hydration: bool = True,
    include_gym_start: bool = True,
    include_post_workout_meal: bool = True,
    include_workout_log: bool = True,
) -> dict:
    """Create a linked pack of one-off Telegram reminders around a planned
    workout: pre-workout meal (90-150 min before), hydration (30-45 min
    before), gym/workout start (at the workout time), post-workout meal
    (45-90 min after the expected end) and a workout-log nudge (after the
    expected end). ``estimated_duration_s`` from the plan sizes the "expected
    end" (default 60 min if the plan doesn't have one).

    ``workout_time`` defaults to the plan's ``planned_start_time`` and
    ``date`` to the plan's day — pass them to override. Idempotent: calling
    this again for the same plan reuses (and resyncs the time of) the
    reminders already linked to it via metadata instead of duplicating them.
    Toggle the ``include_*`` flags to build a partial pack. Returns an error
    naming the missing field if no workout time can be determined."""
    plan = db.get_training_plan(plan_id)
    if plan is None:
        return {"error": f"no training plan with id {plan_id}"}
    workout_date = date or plan.get("day")
    if not workout_date:
        return {"error": "no workout date — the plan has no day and none was provided"}
    time_str = workout_time or plan.get("planned_start_time")
    if not time_str:
        return {
            "error": "workout_time is required — the plan has no planned_start_time; "
                     "pass workout_time (HH:MM)."
        }
    included = {
        "pre_workout_meal": include_pre_workout_meal,
        "hydration": include_hydration,
        "gym_start": include_gym_start,
        "post_workout_meal": include_post_workout_meal,
        "workout_log": include_workout_log,
    }
    wanted_types = [t for t in REMINDER_TYPES if included[t]]
    try:
        times = compute_reminder_pack_times(
            workout_date, time_str, plan.get("estimated_duration_s")
        )
    except ValueError as exc:
        return {"error": str(exc)}

    title = plan.get("title") or "your workout"
    linked = _linked_reminders_by_type(plan_id)
    results: list[dict] = []
    for reminder_type in wanted_types:
        r_date, r_time = times[reminder_type]
        heading, message_tpl = _REMINDER_PACK_COPY[reminder_type]
        message = message_tpl.format(title=title, time=time_str, date=r_date)
        metadata = {
            "plan_id": plan_id, "reminder_type": reminder_type,
            "workout_date": workout_date, "workout_time": time_str,
        }
        existing = linked.get(reminder_type)
        if existing is not None:
            in_sync = existing["time"] == r_time and existing["date"] == r_date
            reminder = existing if in_sync else reminders.edit(
                existing["id"], time=r_time, date=r_date, message=message
            )
            results.append({
                "type": reminder_type, "time": r_time,
                "reminder_id": reminder["id"], "deduplicated": True,
                "_metadata": reminder.get("metadata") or {},
            })
            continue
        created = reminders.create(
            title=heading, message=message, time=r_time, timezone=timezone,
            recurrence="once", date=r_date,
            tags=[_REMINDER_PACK_TAG, reminder_type], metadata=metadata,
        )
        # Defense in depth: Reminders.create() dedupes on (title, message,
        # time, recurrence) only, not date — if it ever returns an unrelated
        # past reminder that slipped through despite the date being baked
        # into the message above, force it back in sync rather than silently
        # keeping its stale date/time.
        was_deduplicated = created.get("deduplicated", False)
        if created["date"] != r_date or created["time"] != r_time:
            created = reminders.edit(created["id"], time=r_time, date=r_date, message=message)
        results.append({
            "type": reminder_type, "time": r_time,
            "reminder_id": created["id"],
            "deduplicated": was_deduplicated,
            "_metadata": created.get("metadata") or {},
        })

    # Cross-reference the whole pack on each reminder so any one of them
    # identifies its siblings — skip the write when it's already correct
    # (the common case on an idempotent re-run).
    all_ids = [r["reminder_id"] for r in results]
    for r in results:
        if sorted(r["_metadata"].get("reminder_pack_ids") or []) == sorted(all_ids):
            continue
        reminders.edit(r["reminder_id"], metadata={
            "plan_id": plan_id, "reminder_type": r["type"],
            "workout_date": workout_date, "workout_time": time_str,
            "reminder_pack_ids": all_ids,
        })
    for r in results:
        del r["_metadata"]

    return {
        "plan_id": plan_id, "date": workout_date, "workout_time": time_str,
        "reminders": results,
    }
def create_telegram_reminder(
    title: str,
    message: str,
    time: str,
    timezone: str = DEFAULT_TIMEZONE,
    recurrence: str = "daily",
    date: str | None = None,
    enabled: bool = True,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Create a Telegram reminder the bot will push at ``time`` (HH:MM, local
    to ``timezone``). recurrence: once | daily | weekly | weekdays | an RRULE
    string (e.g. "RRULE:FREQ=WEEKLY;BYDAY=MO,TH"); "once" requires ``date``
    (YYYY-MM-DD), which also anchors "weekly"/RRULE. The user's replies to
    reminders (meals, /water, /skipped …) land in health events — read them
    with get_health_events. Idempotent: an existing reminder with the same
    title, message, time and recurrence is returned (``deduplicated: true``)
    instead of duplicated. Returns the reminder with its ``id`` and computed
    ``next_run_at`` (UTC)."""
    try:
        return reminders.create(
            title=title, message=message, time=time, timezone=timezone,
            recurrence=recurrence, date=date, enabled=enabled,
            tags=tags, metadata=metadata,
        )
    except ValueError as exc:
        return {"error": str(exc)}
def list_telegram_reminders(
    enabled_only: bool = False, tag: str | None = None
) -> list[dict]:
    """All non-deleted Telegram reminders (id, title, message, time, timezone,
    recurrence, enabled, tags, created_at, updated_at, last_sent_at,
    next_run_at). Filter to active ones with ``enabled_only`` or by ``tag``."""
    return reminders.list(enabled_only=enabled_only, tag=tag)
def edit_telegram_reminder(
    reminder_id: int,
    title: str | None = None,
    message: str | None = None,
    time: str | None = None,
    timezone: str | None = None,
    recurrence: str | None = None,
    date: str | None = None,
    enabled: bool | None = None,
    tags: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Edit an existing reminder in place — never creates a new one. Partial:
    only provided fields change; ``next_run_at`` is recomputed, ``created_at``
    and past delivery records are preserved. Returns the updated reminder."""
    try:
        updated = reminders.edit(
            reminder_id, title=title, message=message, time=time,
            timezone=timezone, recurrence=recurrence, date=date,
            enabled=enabled, tags=tags, metadata=metadata,
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return updated or {"error": f"no reminder with id {reminder_id}"}
def pause_telegram_reminder(reminder_id: int) -> dict:
    """Pause a reminder (enabled=false) without changing its content or
    schedule definition. Returns the updated reminder."""
    updated = reminders.set_enabled(reminder_id, False)
    return updated or {"error": f"no reminder with id {reminder_id}"}
def resume_telegram_reminder(reminder_id: int) -> dict:
    """Resume a paused reminder: enabled=true and ``next_run_at`` recomputed
    so it fires at the next scheduled occurrence (not immediately as
    overdue). Returns the updated reminder."""
    updated = reminders.set_enabled(reminder_id, True)
    return updated or {"error": f"no reminder with id {reminder_id}"}
def delete_telegram_reminder(reminder_id: int) -> dict:
    """Soft-delete a reminder: it never fires again and disappears from
    list_telegram_reminders, but its delivery history is kept."""
    deleted = reminders.delete(reminder_id)
    return (
        {"deleted": True, "reminder_id": reminder_id}
        if deleted else {"error": f"no reminder with id {reminder_id}"}
    )
def send_telegram_message_now(
    message: str, tags: list[str] | None = None, metadata: dict | None = None
) -> dict:
    """Send a Telegram message to the user immediately (not scheduled) — e.g.
    a nudge, a heads-up, or an answer they asked to be pinged with. The
    delivery (or failure) is recorded in the delivery log."""
    try:
        message_id = runtime.send_telegram_message(message)
    except Exception as exc:  # noqa: BLE001 — record + surface, don't crash the tool
        log.exception("Immediate Telegram send failed")
        reminders.record_ad_hoc("error", error=str(exc), tags=tags, metadata=metadata)
        return {"sent": False, "error": str(exc)}
    reminders.record_ad_hoc("sent", telegram_message_id=message_id, tags=tags, metadata=metadata)
    return {"sent": True, "telegram_message_id": message_id}
def create_default_health_reminders(timezone: str = DEFAULT_TIMEZONE) -> list[dict]:
    """Install the recommended Health Coach reminder set: morning plan
    (08:00), lunch log (13:00), dinner log (20:00) and evening report (21:30),
    all daily. Idempotent — reminders that already exist are returned with
    ``deduplicated: true`` instead of duplicated."""
    try:
        return reminders.create_presets(timezone=timezone)
    except ValueError as exc:
        return [{"error": str(exc)}]
def get_reminder_deliveries(reminder_id: int | None = None, limit: int = 30) -> list[dict]:
    """Delivery history (sent / error / missed, with timestamps and error
    text), newest first. Filter to one reminder with ``reminder_id``; ad-hoc
    send_telegram_message_now records have a null reminder_id. Use this to
    check whether reminders are actually reaching Telegram."""
    return reminders.deliveries(reminder_id=reminder_id, limit=max(1, min(limit, 500)))
def get_health_events(days: int = 7, kind: str | None = None) -> list[dict]:
    """Structured events the user logged via Telegram over the last ``days``,
    oldest first: meals (kind=meal), skipped meals (skipped_meal, payload has
    which meal), hydration (added_ml/total_ml), workouts marked done
    (workout_done), and plan/report requests. Use these when writing daily
    reports — they are the ground truth of what the user actually did between
    reminders. Filter with ``kind``."""
    return db.recent_health_events(days=max(1, min(days, 365)), kind=kind)
def start_workout_log_flow(
    plan_id: int, reminder_id: int | None = None, timezone: str = DEFAULT_TIMEZONE
) -> dict:
    """Start the interactive Telegram flow that walks the user through
    logging a finished (or skipped) workout: did you complete it, how long
    did it take, then per-exercise sets/reps/weight/RPE/pain — accepting
    free text and tolerating partial replies. Pushes the first question to
    Telegram now; the user's subsequent replies continue the flow (/cancel,
    /skip, /done are recognised at any step) until it logs the session
    (via the same paths as log_strength_session / mark_plan_done /
    mark_plan_skipped) and sends a final confirmation there. Idempotent:
    calling this again for a plan that already has an open flow resumes it
    instead of starting a second, conflicting conversation."""
    try:
        started = workout_log_flows.start(plan_id, reminder_id=reminder_id, timezone=timezone)
    except ValueError as exc:
        return {"error": str(exc)}
    if not started["reused"]:
        try:
            runtime.send_telegram_message(started["prompt"])
        except Exception as exc:  # noqa: BLE001 — surface, the flow row still exists
            log.exception("Failed to push workout-log flow prompt")
            return {
                "error": f"flow started but the Telegram push failed: {exc}",
                "flow_id": started["flow_id"],
            }
    return {
        "plan_id": plan_id, "flow_id": started["flow_id"],
        "prompt": started["prompt"], "reused": started["reused"],
    }


TOOLS = [create_workout_reminder_pack, create_telegram_reminder, list_telegram_reminders, edit_telegram_reminder, pause_telegram_reminder, resume_telegram_reminder, delete_telegram_reminder, send_telegram_message_now, create_default_health_reminders, get_reminder_deliveries, get_health_events, start_workout_log_flow]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
