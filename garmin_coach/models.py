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


# Map metric-summary column names to the underlying table+model so the
# database layer can validate/route generic queries.
SUMMARY_COLUMNS = {
    "sleep_score", "sleep_hours", "resting_hr", "hrv", "avg_stress",
    "body_battery_high", "body_battery_low", "steps", "weight_kg",
    "body_fat", "hydration_ml", "training_load", "workout_count",
    "calories_in",
}
