"""FastMCP server exposing health tools to ChatGPT Pro.

Mostly read tools over the Garmin-derived metrics, plus a few write tools
(``log_meal``, ``log_weight``) for data Garmin doesn't provide — nutrition,
and manual weight entries (e.g. from Apple Health or a scale ChatGPT is told
about in conversation). ``log_meal`` only ever inserts into the separate
Meal table. ``log_weight`` upserts the same per-day Weight row the Garmin
puller writes: whichever source writes a field last wins, but a write never
blanks fields it doesn't carry (upserts are field-preserving).

Runs over streamable HTTP so a public HTTPS URL can reach it. Auth prefers
OAuth via WorkOS AuthKit (``AUTHKIT_DOMAIN`` + ``MCP_PUBLIC_URL``) — ChatGPT's
connector requires OAuth, and AuthKit is a resource-server-only integration:
WorkOS runs the actual authorization server (login, consent, token issuance),
this process only verifies the JWTs it issues. Sign-up is disabled on the
AuthKit environment and exactly one user is provisioned, so only the owner can
ever complete the login step. Falls back to a static bearer token
(``MCP_BEARER_TOKEN``) if AuthKit isn't configured, for simpler local/test use.

Add the connector in ChatGPT: Settings → Connectors → developer mode → your
public URL; ChatGPT discovers the OAuth flow automatically.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.auth.providers.workos import AuthKitProvider

from .analysis import Analyzer
from .config import settings
from .database import Database, synthetic_activity_id as _synthetic_activity_id
from .garmin_client import GarminClient
from .nutrition_gaps import (
    DEFAULT_TIMEZONE as NUTRITION_TIMEZONE,
    categorize_meals,
    missing_meals as _missing_meals,
    recommend_next_meal,
    sugar_status,
    workout_meal_flags,
)
from .progression import recommend_next_weight, recovery_caution
from .reminders import DEFAULT_TIMEZONE, Reminders
from .telegram_sender import send_telegram_message
from .training_load import estimate_training_load
from .workout_flow import WorkoutLogFlows
from .workout_reminders import REMINDER_TYPES, compute_reminder_pack_times

log = logging.getLogger("garmin_coach.mcp")

db = Database()
analyzer = Analyzer(db)
reminders = Reminders(db)
workout_log_flows = WorkoutLogFlows(db)


def _build_server() -> FastMCP:
    """Construct the FastMCP app, preferring AuthKit OAuth over bearer auth."""
    auth = None
    if settings.authkit_domain and settings.mcp_public_url:
        auth = AuthKitProvider(
            authkit_domain=settings.authkit_domain,
            base_url=settings.mcp_public_url,
        )
    elif settings.mcp_bearer_token:
        auth = StaticTokenVerifier(
            tokens={
                settings.mcp_bearer_token: {
                    "client_id": "chatgpt",
                    "scopes": ["health:read"],
                }
            }
        )
    else:
        log.warning(
            "Neither AUTHKIT_DOMAIN nor MCP_BEARER_TOKEN is set — the server "
            "will be UNAUTHENTICATED. Set one before exposing it publicly."
        )
    return FastMCP(name="Health Coach", auth=auth)


mcp = _build_server()


@mcp.tool
def get_daily_summary(days: int = 14) -> list[dict]:
    """Recent daily health summaries (sleep, HRV, resting HR, stress, steps,
    weight, body fat, training load), oldest first. ``days`` caps the window."""
    return db.daily_summary(days=max(1, min(days, 365)))


@mcp.tool
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


@mcp.tool
def get_training_load(days: int = 28) -> dict:
    """EWMA-weighted acute (7d) vs chronic (28d) training load, their ratio,
    and recent workouts (duplicates excluded). Ratio >1.5 = spike, <0.8 =
    detraining. Each workout's ``load_source`` says whether its load is
    garmin-provided, estimated (documented heuristic), or manually entered."""
    return {
        "acute_chronic": analyzer.acute_chronic_ratio(),
        "recent_workouts": db.recent_workouts(days=days),
    }


@mcp.tool
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


@mcp.tool
def get_flags() -> dict:
    """Current recovery/health flags (e.g. HRV decline, resting-HR jump,
    sleep debt, training-load spike) computed from the latest data."""
    report = analyzer.report()
    return {
        "as_of": report.get("as_of"),
        "flags": report.get("flags", []),
        "available": report.get("available", False),
    }


@mcp.tool
def get_full_report() -> dict:
    """Complete analyzer report in one call: latest daily summary, 7d-vs-28d
    trends for every tracked metric (sleep, HRV, resting HR, steps, stress,
    weight, body fat), sleep debt, acute:chronic training load, and flags."""
    return analyzer.report()


@mcp.tool
def get_feedback(days: int = 30) -> list[dict]:
    """Feedback notes logged via the Telegram coach (/done, /skipped, /felt)
    over the last ``days``, oldest first."""
    return db.recent_feedback(days=max(1, min(days, 365)))


@mcp.tool
def get_latest_plan() -> dict | None:
    """The most recently generated morning plan (day + full plan text, plus
    structured details when available), or null if none exists yet."""
    return db.last_plan()


@mcp.tool
def log_meal(
    name: str,
    calories: int | None = None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    fiber_g: float | None = None,
    sugar_g: float | None = None,
    day: str | None = None,
    note: str = "",
) -> dict:
    """Log a meal (e.g. from a description, photo, or Apple Health nutrition
    entry the user gives you). Macros are optional. ``day`` defaults to today
    (ISO ``YYYY-MM-DD``). Returns the day's meals so far."""
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
        note=note or None,
    )
    return {"logged": True, "day": target_day, "meals_today": db.recent_meals(days=1)}


@mcp.tool
def get_meals(days: int = 7) -> list[dict]:
    """Meals logged over the last ``days``, oldest first."""
    return db.recent_meals(days=max(1, min(days, 365)))


@mcp.tool
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


@mcp.tool
def log_hydration(intake_ml: int, day: str | None = None, goal_ml: int | None = None) -> dict:
    """Log water/fluid intake for a day (e.g. from Apple Health). ``day``
    defaults to today. Updates the provided fields on that day's entry (a
    later Garmin sync may update them again)."""
    target_day = day or date.today().isoformat()
    db.upsert_hydration(target_day, intake_ml=intake_ml, goal_ml=goal_ml)
    return {"logged": True, "day": target_day, "intake_ml": intake_ml}


@mcp.tool
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


@mcp.tool
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


@mcp.tool
def get_vitals(metric: str | None = None, days: int = 30) -> list[dict]:
    """Vitals logged via log_vital over the last ``days``, oldest first.
    Pass ``metric`` to filter to one named metric, or omit for all."""
    return db.recent_vitals(metric=metric, days=max(1, min(days, 365)))


@mcp.tool
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


# ── Garmin sync ─────────────────────────────────────────────────────────────────

# One authenticated client per process, created on first use so the server
# boots fine when Garmin auth is broken — only the sync tools then fail.
_garmin: GarminClient | None = None

# Don't re-hit ~9 Garmin endpoints because a chatty conversation asked twice;
# the hourly scheduler sync makes anything fresher than this rarely useful.
_SYNC_COOLDOWN_MIN = 10


def _garmin_client() -> GarminClient:
    global _garmin
    if _garmin is None:
        _garmin = GarminClient(db)
    return _garmin


def _minutes_since(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # stored as UTC
    return round((datetime.now(timezone.utc) - ts).total_seconds() / 60, 1)


@mcp.tool
def sync_garmin(day: str | None = None, force: bool = False) -> dict:
    """Pull fresh data from Garmin Connect right now instead of waiting for
    the hourly sync — use before answering when get_sync_status shows stale
    data. Defaults to today; pass ``day`` (YYYY-MM-DD) to refresh a specific
    date. Skipped if today was already synced within the last few minutes
    unless ``force=true``. Returns per-metric results."""
    last = db.last_pull()
    if day is None and not force and last is not None:
        minutes_ago = _minutes_since(last["ts"])
        if minutes_ago is not None and minutes_ago < _SYNC_COOLDOWN_MIN:
            return {
                "synced": False,
                "reason": f"already synced {minutes_ago:g} minutes ago — "
                          "data is current (use force=true to override)",
                "last_synced_day": last["day"],
                "minutes_since_last_sync": minutes_ago,
            }
    target_day = day or date.today().isoformat()
    try:
        results = _garmin_client().pull_day(target_day)
    except Exception as exc:  # noqa: BLE001 — surface auth/network problems to the caller
        log.exception("On-demand Garmin sync failed")
        return {
            "synced": False,
            "error": f"Garmin sync failed: {exc}",
            "hint": "If this persists, the cached Garmin tokens may have "
                    "expired — run scripts/garmin_login.py on the server.",
        }
    ok = sum(1 for status in results.values() if status == "ok")
    return {
        "synced": ok > 0,
        "day": target_day,
        "metrics_ok": ok,
        "metrics_failed": len(results) - ok,
        "results": results,
    }


@mcp.tool
def get_sync_status() -> dict:
    """When Garmin data was last synced: the day covered, how many minutes
    ago the sync ran, its per-metric results, and whether it's stale (the
    scheduler syncs hourly, so >90 minutes means something is wrong). Check
    this before trusting today's numbers; use sync_garmin to refresh."""
    last = db.last_pull()
    if last is None:
        return {
            "synced_ever": False,
            "detail": "No Garmin pull recorded yet — run a backfill or sync_garmin.",
        }
    minutes_ago = _minutes_since(last["ts"])
    return {
        "synced_ever": True,
        "last_synced_day": last["day"],
        "last_synced_at": last["ts"].isoformat() if last["ts"] else None,
        "minutes_since_last_sync": minutes_ago,
        "stale": minutes_ago is None or minutes_ago > 90,
        "last_results": last["status"],
    }


# ── Profile / goals ─────────────────────────────────────────────────────────────

@mcp.tool
def get_profile() -> dict | None:
    """The user's profile and goals (age, sex, height, target weight, goal
    type, training level, injuries, equipment, food restrictions, calorie/
    macro targets). Read this before recommending training or food. Returns
    null if no profile has been set yet."""
    return db.get_profile()


@mcp.tool
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
    notes: str | None = None,
    replace: bool = False,
) -> dict:
    """Create or update the user profile. Partial by default — only the
    fields you pass change. Set ``replace=True`` only when the user
    explicitly wants the whole profile rewritten. goal_type: fat_loss |
    muscle_gain | recomposition | endurance | general_health; training_level:
    beginner | intermediate | advanced."""
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
        fat_target_g=fat_target_g, fiber_target_g=fiber_target_g, notes=notes,
    )


# ── Strength training ───────────────────────────────────────────────────────────

@mcp.tool
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


@mcp.tool
def get_strength_sessions(days: int = 30) -> list[dict]:
    """Strength sessions (with exercises) over the last ``days``, oldest
    first."""
    return db.recent_strength_sessions(days=max(1, min(days, 365)))


@mcp.tool
def get_exercise_history(exercise_name: str, days: int = 180, limit: int = 20) -> list[dict]:
    """Past performances of one exercise, newest first: date, sets, reps,
    weight, estimated volume, best set, RPE/RIR. Use to check progressive
    overload and pick today's working weight."""
    return db.exercise_history(exercise_name, days=max(1, min(days, 730)), limit=limit)


@mcp.tool
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


@mcp.tool
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
        rec = recommend_next_weight(history[0], caution)
        rec["exercise"] = name
        rec["last_trained"] = history[0]["date"]
        recommendations.append(rec)
    return {"recovery_caution": caution, "recommendations": recommendations}


@mcp.tool
def delete_strength_session(session_id: int) -> dict:
    """Delete a strength session, its exercises, and its mirrored workout
    row (daily summaries recompute automatically)."""
    deleted = db.delete_strength_session(session_id)
    return {"deleted": deleted, "session_id": session_id}


# ── Planned workouts and adherence ──────────────────────────────────────────────

@mcp.tool
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


@mcp.tool
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


@mcp.tool
def get_training_plan(plan_id: int) -> dict:
    """A single training plan by id, including its edit history (what
    changed and when)."""
    plan = db.get_training_plan(plan_id)
    if plan is None:
        return {"error": f"no training plan with id {plan_id}"}
    plan["history"] = db.training_plan_history(plan_id)
    return plan


@mcp.tool
def get_today_plan() -> list[dict]:
    """Today's planned workout(s) with status (planned / done / skipped /
    partially_done). Empty list if nothing is planned."""
    return db.get_today_training_plans()


@mcp.tool
def get_training_plans(days: int = 14, status: str | None = None) -> list[dict]:
    """Training plans over the last ``days`` (oldest first), optionally
    filtered by status — e.g. status='skipped' to see what keeps not
    happening."""
    return db.get_training_plans(days=max(1, min(days, 365)), status=status)


@mcp.tool
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


@mcp.tool
def mark_plan_skipped(plan_id: int, reason: str | None = None) -> dict:
    """Mark a training plan skipped, with the reason (tired, sick, no time…)
    — the reason matters for tomorrow's recommendation."""
    updated = db.update_training_plan(plan_id, status="skipped", skip_reason=reason)
    return updated or {"error": f"no training plan with id {plan_id}"}


@mcp.tool
def log_plan_feedback(plan_id: int, feedback: str) -> dict:
    """Attach free-text feedback to a training plan (how it went, what to
    change next time)."""
    updated = db.update_training_plan(plan_id, feedback=feedback)
    return updated or {"error": f"no training plan with id {plan_id}"}


# ── Workout reminder packs ───────────────────────────────────────────────────────

_REMINDER_PACK_TAG = "workout_reminder_pack"

# (title, message template — {title} is the plan's title, {time} its HH:MM start)
_REMINDER_PACK_COPY: dict[str, tuple[str, str]] = {
    "pre_workout_meal": (
        "Pre-workout meal",
        "Eat a pre-workout meal before {title} at {time}.",
    ),
    "hydration": (
        "Hydration",
        "Hydrate before {title} at {time} — water/electrolytes now.",
    ),
    "gym_start": ("Workout time", "Time to start: {title}."),
    "post_workout_meal": (
        "Post-workout meal",
        "Eat your post-workout meal — protein + carbs to kick off recovery.",
    ),
    "workout_log": (
        "Log your workout",
        "Log how {title} went (sets/reps/RPE) so tomorrow's plan can adjust.",
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


@mcp.tool
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
        message = message_tpl.format(title=title, time=time_str)
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
        results.append({
            "type": reminder_type, "time": r_time,
            "reminder_id": created["id"],
            "deduplicated": created.get("deduplicated", False),
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


# ── Nutrition summary ───────────────────────────────────────────────────────────

@mcp.tool
def get_nutrition_summary(days: int = 7, day: str | None = None) -> list[dict]:
    """Per-day nutrition totals (calories + macros), meal count, the meals
    themselves, and — when a profile sets targets — calories/protein
    remaining. Pass ``day`` for a single date. Use for 'what should I eat
    next today?'. Meals logged without macros contribute only what they
    carry."""
    return db.nutrition_summary(days=max(1, min(days, 90)), day=day)


@mcp.tool
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


# ── Meal / workout corrections ──────────────────────────────────────────────────

@mcp.tool
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


@mcp.tool
def delete_meal(meal_id: int) -> dict:
    """Delete a logged meal. Returns the day's remaining meals."""
    day = db.delete_meal(meal_id)
    if day is None:
        return {"error": f"no meal with id {meal_id}"}
    return {"deleted": True, "day": day, "meals": db.meals_for_day(day)}


@mcp.tool
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


@mcp.tool
def delete_workout(activity_id: int) -> dict:
    """Delete a workout (e.g. a wrong or duplicated entry). Returns the
    day's remaining workouts; daily summaries recompute automatically."""
    day = db.delete_workout(activity_id)
    if day is None:
        return {"error": f"no workout with activity_id {activity_id}"}
    return {"deleted": True, "day": day, "workouts": db.workouts_for_day(day)}


# ── Workout deduplication ───────────────────────────────────────────────────────

@mcp.tool
def get_duplicate_workouts(days: int = 60) -> list[list[dict]]:
    """Groups of same-day activities that look like one workout recorded by
    multiple sources (similar duration/distance/calories). Detection only —
    review before calling dedupe_workouts."""
    return db.find_duplicate_workouts(days=max(1, min(days, 365)))


@mcp.tool
def dedupe_workouts(days: int = 60) -> dict:
    """Soft-delete duplicates: in each group the highest-priority source
    (default garmin > apple > manual; see set_activity_source_priority) is
    kept and the rest are marked ``duplicate_of`` the keeper. Marked rows
    disappear from summaries and training load but stay in the database
    (no hard delete). Returns what was marked."""
    return db.dedupe_workouts(days=max(1, min(days, 365)))


@mcp.tool
def set_activity_source_priority(priority: list[str]) -> dict:
    """Which source wins when the same workout exists twice, highest first —
    e.g. ["garmin", "apple", "manual"]."""
    profile = db.set_profile(activity_source_priority=",".join(priority))
    return {"activity_source_priority": profile["activity_source_priority"]}


@mcp.tool
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
    merged row instead of creating another one."""
    return db.merge_garmin_strength_fragments(
        day, dry_run=dry_run,
        min_fragments=max(1, min_fragments),
        max_gap_minutes=max(1.0, max_gap_minutes),
    )


# ── Readiness check-in ──────────────────────────────────────────────────────────

@mcp.tool
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


@mcp.tool
def get_readiness(days: int = 14) -> list[dict]:
    """Readiness check-ins over the last ``days``, oldest first."""
    return db.recent_readiness(days=max(1, min(days, 365)))


# ── Body measurements ───────────────────────────────────────────────────────────

@mcp.tool
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


@mcp.tool
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


# ── Hydration reads ─────────────────────────────────────────────────────────────

@mcp.tool
def get_hydration(days: int = 14) -> list[dict]:
    """Daily hydration entries (intake, goal, percent of goal) over the last
    ``days``, oldest first. Days never logged are absent."""
    return db.recent_hydration(days=max(1, min(days, 365)))


@mcp.tool
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


# ── Telegram reminders ──────────────────────────────────────────────────────────

@mcp.tool
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


@mcp.tool
def list_telegram_reminders(
    enabled_only: bool = False, tag: str | None = None
) -> list[dict]:
    """All non-deleted Telegram reminders (id, title, message, time, timezone,
    recurrence, enabled, tags, created_at, updated_at, last_sent_at,
    next_run_at). Filter to active ones with ``enabled_only`` or by ``tag``."""
    return reminders.list(enabled_only=enabled_only, tag=tag)


@mcp.tool
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


@mcp.tool
def pause_telegram_reminder(reminder_id: int) -> dict:
    """Pause a reminder (enabled=false) without changing its content or
    schedule definition. Returns the updated reminder."""
    updated = reminders.set_enabled(reminder_id, False)
    return updated or {"error": f"no reminder with id {reminder_id}"}


@mcp.tool
def resume_telegram_reminder(reminder_id: int) -> dict:
    """Resume a paused reminder: enabled=true and ``next_run_at`` recomputed
    so it fires at the next scheduled occurrence (not immediately as
    overdue). Returns the updated reminder."""
    updated = reminders.set_enabled(reminder_id, True)
    return updated or {"error": f"no reminder with id {reminder_id}"}


@mcp.tool
def delete_telegram_reminder(reminder_id: int) -> dict:
    """Soft-delete a reminder: it never fires again and disappears from
    list_telegram_reminders, but its delivery history is kept."""
    deleted = reminders.delete(reminder_id)
    return (
        {"deleted": True, "reminder_id": reminder_id}
        if deleted else {"error": f"no reminder with id {reminder_id}"}
    )


@mcp.tool
def send_telegram_message_now(
    message: str, tags: list[str] | None = None, metadata: dict | None = None
) -> dict:
    """Send a Telegram message to the user immediately (not scheduled) — e.g.
    a nudge, a heads-up, or an answer they asked to be pinged with. The
    delivery (or failure) is recorded in the delivery log."""
    try:
        message_id = send_telegram_message(message)
    except Exception as exc:  # noqa: BLE001 — record + surface, don't crash the tool
        log.exception("Immediate Telegram send failed")
        reminders.record_ad_hoc("error", error=str(exc), tags=tags, metadata=metadata)
        return {"sent": False, "error": str(exc)}
    reminders.record_ad_hoc("sent", telegram_message_id=message_id, tags=tags, metadata=metadata)
    return {"sent": True, "telegram_message_id": message_id}


@mcp.tool
def create_default_health_reminders(timezone: str = DEFAULT_TIMEZONE) -> list[dict]:
    """Install the recommended Health Coach reminder set: morning plan
    (08:00), lunch log (13:00), dinner log (20:00) and evening report (21:30),
    all daily. Idempotent — reminders that already exist are returned with
    ``deduplicated: true`` instead of duplicated."""
    try:
        return reminders.create_presets(timezone=timezone)
    except ValueError as exc:
        return [{"error": str(exc)}]


@mcp.tool
def get_reminder_deliveries(reminder_id: int | None = None, limit: int = 30) -> list[dict]:
    """Delivery history (sent / error / missed, with timestamps and error
    text), newest first. Filter to one reminder with ``reminder_id``; ad-hoc
    send_telegram_message_now records have a null reminder_id. Use this to
    check whether reminders are actually reaching Telegram."""
    return reminders.deliveries(reminder_id=reminder_id, limit=max(1, min(limit, 500)))


# ── Health events (Telegram-captured logs) ──────────────────────────────────────

@mcp.tool
def get_health_events(days: int = 7, kind: str | None = None) -> list[dict]:
    """Structured events the user logged via Telegram over the last ``days``,
    oldest first: meals (kind=meal), skipped meals (skipped_meal, payload has
    which meal), hydration (added_ml/total_ml), workouts marked done
    (workout_done), and plan/report requests. Use these when writing daily
    reports — they are the ground truth of what the user actually did between
    reminders. Filter with ``kind``."""
    return db.recent_health_events(days=max(1, min(days, 365)), kind=kind)


# ── Telegram workout-completion flow ─────────────────────────────────────────────

@mcp.tool
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
            send_telegram_message(started["prompt"])
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


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("Starting MCP server on %s:%s", settings.mcp_host, settings.mcp_port)
    mcp.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
