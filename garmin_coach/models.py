"""SQLModel table definitions — the schema for the whole system.

Every metric family is its own table keyed by ``day`` (an ISO ``YYYY-MM-DD``
string) so a re-pull of the same day updates rather than duplicates. Workouts are
keyed by Garmin's ``activity_id``; conversation/feedback rows get an
autoincrement id. A ``daily_summary`` SQL view (created in :mod:`database`)
stitches the families together for reads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Sleep(SQLModel, table=True):
    day: str = Field(primary_key=True)
    score: Optional[int] = None
    total_seconds: Optional[int] = None
    deep_seconds: Optional[int] = None
    light_seconds: Optional[int] = None
    rem_seconds: Optional[int] = None
    awake_seconds: Optional[int] = None
    resting_hr: Optional[int] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class Hrv(SQLModel, table=True):
    day: str = Field(primary_key=True)
    last_night_avg: Optional[int] = None
    weekly_avg: Optional[int] = None
    status: Optional[str] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class RestingHr(SQLModel, table=True):
    __tablename__ = "resting_hr"
    day: str = Field(primary_key=True)
    resting_hr: Optional[int] = None
    min_hr: Optional[int] = None
    max_hr: Optional[int] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class Stress(SQLModel, table=True):
    day: str = Field(primary_key=True)
    avg_stress: Optional[int] = None
    max_stress: Optional[int] = None
    rest_seconds: Optional[int] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class BodyBattery(SQLModel, table=True):
    __tablename__ = "body_battery"
    day: str = Field(primary_key=True)
    high: Optional[int] = None
    low: Optional[int] = None
    charged: Optional[int] = None
    drained: Optional[int] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class Steps(SQLModel, table=True):
    day: str = Field(primary_key=True)
    steps: Optional[int] = None
    goal: Optional[int] = None
    distance_m: Optional[float] = None
    calories: Optional[int] = None
    floors_climbed: Optional[int] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class Workout(SQLModel, table=True):
    activity_id: int = Field(primary_key=True)
    day: str = Field(index=True)
    name: Optional[str] = None
    type: Optional[str] = None
    duration_s: Optional[float] = None
    distance_m: Optional[float] = None
    calories: Optional[int] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    training_load: Optional[float] = None
    # Provenance: where the row came from (garmin | apple | manual | garmin_merged)
    # and where its training_load came from (garmin | estimated | manual).
    source: Optional[str] = None
    load_source: Optional[str] = None
    # Soft-delete marker for deduplication: points at the kept activity_id.
    # Summaries, training load and default workout reads skip marked rows.
    # Also reused by merge_garmin_strength_fragments to point same-day Garmin
    # strength fragments at their canonical "garmin_merged" row.
    duplicate_of: Optional[int] = None
    # Garmin's startTimeLocal (as returned, e.g. "2026-07-04 18:32:00") — the
    # only per-workout timestamp we have; used to group same-day strength
    # fragments that belong to one gym session. Null for manual/legacy rows.
    start_time: Optional[str] = None
    # Free-form JSON provenance, e.g. {"fragment_ids": [...]} on a merged row,
    # or {"linked": [...]} on a field-level "merged" canonical.
    meta_json: Optional[str] = None
    # Per-field provenance JSON on a field-level "merged" canonical, e.g.
    # {"exercise_details": "manual", "avg_hr": "garmin", "calories": "garmin"}.
    # Null on ordinary single-source rows. See workout_merge.merge_fields.
    field_sources: Optional[str] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class WorkoutSourceLink(SQLModel, table=True):
    """Links a source workout row (manual/garmin/apple) to the field-level
    ``merged`` canonical it contributes to.

    Row-level dedupe just points a duplicate at its keeper via
    ``Workout.duplicate_of``; that still happens (so summaries/training load
    count the session once), but this table additionally records *why* the two
    were linked and *which* canonical fields each source provided — so a merge
    is auditable, reversible (``unmerge_workout_sources``), and idempotent
    (a repeated Garmin sync updates the existing link instead of adding one).
    """

    __tablename__ = "workout_source_link"
    id: Optional[int] = Field(default=None, primary_key=True)
    canonical_activity_id: int = Field(index=True)
    source_activity_id: int = Field(index=True)
    source: str  # manual | garmin | apple
    match_confidence: Optional[float] = None
    match_reason: Optional[str] = None
    fields_imported: Optional[str] = None  # JSON array of canonical fields
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Weight(SQLModel, table=True):
    day: str = Field(primary_key=True)
    weight_kg: Optional[float] = None
    body_fat: Optional[float] = None
    muscle_kg: Optional[float] = None
    body_water: Optional[float] = None
    bmi: Optional[float] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class Hydration(SQLModel, table=True):
    day: str = Field(primary_key=True)
    intake_ml: Optional[int] = None
    goal_ml: Optional[int] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class Meal(SQLModel, table=True):
    """User-logged nutrition (via MCP tool or Telegram) — not pulled from Garmin.

    Macros are all nullable so a calories-only meal is valid and nutrition
    totals only ever sum the values that are actually present (see
    ``Database.nutrition_summary``). ``source``/``source_record_id`` give the
    row provenance (``manual`` | ``apple`` | ``telegram`` | ...) and let an
    importer dedupe idempotently on the originating record's id.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    day: str = Field(index=True)
    ts: datetime = Field(default_factory=_utcnow)
    name: str
    calories: Optional[int] = None
    protein_g: Optional[float] = None
    carbs_g: Optional[float] = None
    fat_g: Optional[float] = None
    fiber_g: Optional[float] = None
    sugar_g: Optional[float] = None
    sodium_mg: Optional[float] = None
    # Provenance (all nullable — legacy rows predate these columns).
    source: Optional[str] = None  # manual | apple | telegram | import
    source_record_id: Optional[str] = None  # id in the originating system
    is_estimated: Optional[bool] = None
    note: Optional[str] = None


class Vital(SQLModel, table=True):
    """Generic named biometric reading not covered by a dedicated table —
    e.g. blood pressure, blood glucose, oxygen saturation, height. Imported
    from Apple Health or any other source a user describes to the coach."""

    id: Optional[int] = Field(default=None, primary_key=True)
    day: str = Field(index=True)
    ts: datetime = Field(default_factory=_utcnow)
    metric: str = Field(index=True)
    value: float
    unit: Optional[str] = None
    note: Optional[str] = None


class Conversation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_utcnow, index=True)
    role: str  # system | user | assistant
    content: str


class Feedback(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    day: str = Field(index=True)
    ts: datetime = Field(default_factory=_utcnow)
    note: str


class Plan(SQLModel, table=True):
    day: str = Field(primary_key=True)
    ts: datetime = Field(default_factory=_utcnow)
    plan: str


class PlanDetail(SQLModel, table=True):
    """Structured form of a morning plan (priorities/workout/tip as JSON).

    Kept separate from :class:`Plan` (which stores the rendered text) so the
    existing table needs no migration and text-only fallback plans still save.
    """

    __tablename__ = "plan_detail"
    day: str = Field(primary_key=True)
    ts: datetime = Field(default_factory=_utcnow)
    data: str  # JSON: {"priorities": [...], "workout": {...}, "recovery_tip": ...}


class PullLog(SQLModel, table=True):
    """One row per day successfully pulled from Garmin.

    Lets the scheduler distinguish "never pulled" from "pulled but the watch
    had no data", so it can heal gaps (e.g. an interrupted backfill) without
    re-pulling genuinely empty days forever.
    """

    __tablename__ = "pull_log"
    day: str = Field(primary_key=True)
    ts: datetime = Field(default_factory=_utcnow)
    status: Optional[str] = None  # JSON of per-metric results from pull_day


class Profile(SQLModel, table=True):
    """Single-row user profile and goals (id is always 1) — what the coach
    reads before recommending training or food. Every field optional."""

    id: int = Field(default=1, primary_key=True)
    age: Optional[int] = None
    sex: Optional[str] = None
    height_cm: Optional[float] = None
    current_weight_kg: Optional[float] = None
    target_weight_kg: Optional[float] = None
    goal_type: Optional[str] = None  # fat_loss|muscle_gain|recomposition|endurance|general_health
    training_level: Optional[str] = None  # beginner|intermediate|advanced
    injuries_or_limitations: Optional[str] = None
    available_equipment: Optional[str] = None
    preferred_training_days: Optional[str] = None
    food_restrictions: Optional[str] = None
    calorie_target: Optional[int] = None
    protein_target_g: Optional[float] = None
    carbs_target_g: Optional[float] = None
    fat_target_g: Optional[float] = None
    fiber_target_g: Optional[float] = None
    notes: Optional[str] = None
    # Comma-separated dedupe preference, e.g. "garmin,apple,manual".
    activity_source_priority: Optional[str] = None
    # ── Configurable sleep need (no more hard-coded 8 hours) ────────────────
    # sleep_target_hours is the debt reference; the preferred band and the
    # minimum-recovery floor feed recovery classification. ``effective_from``
    # records when the *current* target took effect; historical, effective-
    # dated targets live in ``SleepTargetHistory`` so old sleep-debt numbers
    # stay reproducible after a change.
    sleep_target_hours: Optional[float] = None
    sleep_preferred_min_hours: Optional[float] = None
    sleep_preferred_max_hours: Optional[float] = None
    sleep_minimum_recovery_hours: Optional[float] = None
    sleep_target_effective_from: Optional[str] = None  # YYYY-MM-DD
    # ── Persistent hydration configuration ──────────────────────────────────
    hydration_baseline_target_ml: Optional[int] = None
    hydration_training_day_target_ml: Optional[int] = None
    hydration_hot_day_target_ml: Optional[int] = None
    hydration_medical_limit_note: Optional[str] = None
    updated_at: datetime = Field(default_factory=_utcnow)


class SleepTargetHistory(SQLModel, table=True):
    """Effective-dated sleep-target changes so historical sleep-debt numbers
    stay reproducible after the user updates their target.

    ``sleep_target_for(day)`` returns the target whose ``effective_from`` is the
    latest one on-or-before ``day``. One row is appended per change; the current
    value is mirrored onto ``Profile.sleep_target_hours`` for quick reads.
    """

    __tablename__ = "sleep_target_history"
    id: Optional[int] = Field(default=None, primary_key=True)
    effective_from: str = Field(index=True)  # YYYY-MM-DD
    target_hours: float
    minimum_recovery_hours: Optional[float] = None
    note: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)


class FeatureRequest(SQLModel, table=True):
    """First-class server-side feature backlog — so requirements live in a
    queryable table with status/priority instead of only free-text profile
    notes. CRUD via the MCP feature-request tools."""

    __tablename__ = "feature_request"
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: Optional[str] = None
    priority: Optional[str] = None  # low | medium | high
    status: str = "requested"  # requested|planned|in_progress|blocked|implemented|rejected
    requested_by: Optional[str] = None
    related_endpoint: Optional[str] = None
    resolution_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class StrengthSession(SQLModel, table=True):
    """A detailed strength workout; per-exercise rows live in
    StrengthExercise. Mirrored into Workout (via activity_id) so sessions
    show up in workout history and training load."""

    __tablename__ = "strength_session"
    id: Optional[int] = Field(default=None, primary_key=True)
    day: str = Field(index=True)
    ts: datetime = Field(default_factory=_utcnow)
    session_name: Optional[str] = None
    gym: Optional[str] = None  # location, e.g. "Sports Center"
    duration_s: Optional[float] = None
    calories: Optional[int] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    notes: Optional[str] = None
    activity_id: Optional[int] = None  # linked Workout row


class StrengthExercise(SQLModel, table=True):
    """One exercise within a strength session.

    ``sets``/``reps``/``weight_kg``/``rpe`` are the working-set aggregate (top
    weight, average reps/RPE) — kept even when per-set data exists so history
    queries stay simple. ``set_details`` holds the full per-set JSON
    (``[{"reps": 10, "weight_kg": 80, "rpe": 7}, ...]``) when the user logs
    set-by-set. ``planned_*`` capture the prescription so planned-vs-actual
    comparison works; ``status`` records completed/skipped/substituted.
    """

    __tablename__ = "strength_exercise"
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, foreign_key="strength_session.id")
    exercise_name: str = Field(index=True)
    machine: Optional[str] = None  # equipment used, e.g. "45° leg press sled"
    planned_sets: Optional[int] = None
    planned_reps: Optional[str] = None  # a range like "8-10" is fine
    planned_weight_kg: Optional[float] = None
    sets: Optional[int] = None
    reps: Optional[int] = None
    weight_kg: Optional[float] = None
    rpe: Optional[float] = None
    rir: Optional[float] = None
    rest_s: Optional[float] = None
    set_details: Optional[str] = None  # JSON array of per-set dicts
    status: Optional[str] = None  # completed | skipped | substituted
    substitute_exercise: Optional[str] = None
    completed: Optional[bool] = None
    pain_note: Optional[str] = None
    notes: Optional[str] = None


class TrainingPlan(SQLModel, table=True):
    """A planned workout for a specific day, with adherence tracking.

    Distinct from Plan (the generated morning briefing text): this is the
    committable workout the user agreed to, whose done/skipped status feeds
    tomorrow's recommendation.
    """

    __tablename__ = "training_plan"
    id: Optional[int] = Field(default=None, primary_key=True)
    day: str = Field(index=True)
    ts: datetime = Field(default_factory=_utcnow)
    title: Optional[str] = None
    goal: Optional[str] = None
    planned_start_time: Optional[str] = None  # "HH:MM"
    estimated_duration_s: Optional[float] = None
    workout_type: Optional[str] = None
    exercises: Optional[str] = None  # JSON array
    cardio_plan: Optional[str] = None
    intensity_target: Optional[str] = None
    notes: Optional[str] = None
    status: str = "planned"  # planned|done|skipped|partially_done
    feedback: Optional[str] = None
    actual_duration_s: Optional[float] = None
    difficulty_rpe: Optional[float] = None
    skip_reason: Optional[str] = None
    updated_at: Optional[datetime] = None


class TrainingPlanEdit(SQLModel, table=True):
    """Audit trail for update_training_plan: one row per call that actually
    changed a field, so "why did today's plan change" is answerable later."""

    __tablename__ = "training_plan_edit"
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: int = Field(index=True, foreign_key="training_plan.id")
    ts: datetime = Field(default_factory=_utcnow)
    changes_json: str  # {"field": {"old": ..., "new": ...}, ...}


class Readiness(SQLModel, table=True):
    """Subjective daily check-in, combined with HRV/sleep/load for coaching."""

    day: str = Field(primary_key=True)
    ts: datetime = Field(default_factory=_utcnow)
    energy_1_10: Optional[int] = None
    soreness_1_10: Optional[int] = None
    motivation_1_10: Optional[int] = None
    sleep_quality_1_10: Optional[int] = None
    stress_1_10: Optional[int] = None
    mood: Optional[str] = None
    pain_areas: Optional[str] = None
    notes: Optional[str] = None


class BodyMeasurement(SQLModel, table=True):
    """Tape measurements — tracks recomposition better than weight alone."""

    __tablename__ = "body_measurement"
    day: str = Field(primary_key=True)
    ts: datetime = Field(default_factory=_utcnow)
    waist_cm: Optional[float] = None
    chest_cm: Optional[float] = None
    neck_cm: Optional[float] = None
    arm_cm: Optional[float] = None
    thigh_cm: Optional[float] = None
    hip_cm: Optional[float] = None
    notes: Optional[str] = None


class TelegramReminder(SQLModel, table=True):
    """A scheduled Telegram nudge (meal logging, plans, reports) managed by
    ChatGPT via the MCP tools or by the user via bot commands.

    ``time`` is local wall-clock ("HH:MM") in ``timezone``; ``next_run_at`` is
    the precomputed next fire time in UTC, refreshed by the dispatch loop after
    every send and by edits/resume. Soft-deleted rows (``deleted_at`` set)
    never fire but keep their delivery history.
    """

    __tablename__ = "telegram_reminder"
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    message: str
    time: str  # "HH:MM" local wall-clock in `timezone`
    timezone: str = "Asia/Jerusalem"
    recurrence: str = "daily"  # once | daily | weekly | weekdays | RRULE:...
    date: Optional[str] = None  # YYYY-MM-DD; required for "once", anchors "weekly"/RRULE
    enabled: bool = True
    tags_json: Optional[str] = None  # JSON array of strings
    metadata_json: Optional[str] = None  # free-form JSON object
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_sent_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    deleted_at: Optional[datetime] = None


class TelegramReminderDelivery(SQLModel, table=True):
    """One delivery attempt of a reminder (or an ad-hoc send when
    ``reminder_id`` is null): sent, error, or missed. Kept forever — editing
    or deleting a reminder never touches its history."""

    __tablename__ = "telegram_reminder_delivery"
    id: Optional[int] = Field(default=None, primary_key=True)
    reminder_id: Optional[int] = Field(default=None, index=True)
    sent_at: datetime = Field(default_factory=_utcnow)
    status: str  # sent | error | missed
    telegram_message_id: Optional[int] = None
    error: Optional[str] = None
    meta_json: Optional[str] = None  # tags/metadata for ad-hoc sends


class HealthEvent(SQLModel, table=True):
    """Structured event captured from the user's Telegram replies (meal
    logged/skipped, hydration, workout done, report/plan requests) so
    ChatGPT/the coach can read back what actually happened during the day."""

    __tablename__ = "health_event"
    id: Optional[int] = Field(default=None, primary_key=True)
    day: str = Field(index=True)
    ts: datetime = Field(default_factory=_utcnow)
    kind: str = Field(index=True)  # meal | skipped_meal | hydration | workout_done | free_text | report_request | plan_request
    source: str = "telegram"
    payload_json: Optional[str] = None


class WorkoutLogFlow(SQLModel, table=True):
    """State for the Telegram guided workout-completion conversation, so it
    survives across separate incoming messages. One open (``completed_at``
    null) flow per plan at a time — ``start_workout_log_flow`` reuses it
    instead of starting a second, conflicting conversation."""

    __tablename__ = "workout_log_flow"
    id: Optional[int] = Field(default=None, primary_key=True)
    plan_id: int = Field(index=True)
    reminder_id: Optional[int] = None
    timezone: str = "Asia/Jerusalem"
    # awaiting_completion|awaiting_skip_reason|awaiting_duration|awaiting_exercises|done
    step: str = "awaiting_completion"
    completion_status: Optional[str] = None  # yes|partial|skipped
    duration_s: Optional[float] = None
    exercises_json: Optional[str] = None  # JSON array of exercises collected so far
    current_exercise_index: int = 0
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    completed_at: Optional[datetime] = None
    result_json: Optional[str] = None  # final confirmation summary, once done


# Map metric-summary column names to the underlying table+model so the
# database layer can validate/route generic queries.
SUMMARY_COLUMNS = {
    "sleep_score", "sleep_hours", "resting_hr", "hrv", "avg_stress",
    "body_battery_high", "body_battery_low", "steps", "weight_kg",
    "body_fat", "hydration_ml", "training_load", "workout_count",
    "calories_in",
}
