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
    # Provenance: where the row came from (garmin | apple | manual) and where
    # its training_load came from (garmin | estimated | manual).
    source: Optional[str] = None
    load_source: Optional[str] = None
    # Soft-delete marker for deduplication: points at the kept activity_id.
    # Summaries, training load and default workout reads skip marked rows.
    duplicate_of: Optional[int] = None
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
    """User-logged nutrition (via MCP tool or Telegram) — not pulled from Garmin."""

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
    duration_s: Optional[float] = None
    calories: Optional[int] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    notes: Optional[str] = None
    activity_id: Optional[int] = None  # linked Workout row


class StrengthExercise(SQLModel, table=True):
    __tablename__ = "strength_exercise"
    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(index=True, foreign_key="strength_session.id")
    exercise_name: str = Field(index=True)
    sets: Optional[int] = None
    reps: Optional[int] = None
    weight_kg: Optional[float] = None
    rpe: Optional[float] = None
    rir: Optional[float] = None
    rest_s: Optional[float] = None
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


# Map metric-summary column names to the underlying table+model so the
# database layer can validate/route generic queries.
SUMMARY_COLUMNS = {
    "sleep_score", "sleep_hours", "resting_hr", "hrv", "avg_stress",
    "body_battery_high", "body_battery_low", "steps", "weight_kg",
    "body_fat", "hydration_ml", "training_load", "workout_count",
    "calories_in",
}
