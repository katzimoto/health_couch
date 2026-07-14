"""MetricsMixin: Garmin-sourced metric upserts, the daily-summary reads, readiness,
body measurements, and hydration reads.

Extracted from :class:`garmin_coach.database.Database` as a mixin; the
composed ``Database`` provides shared primitives (``session``, ``_upsert``,
``_cutoff``, ``_view_rows``). Not instantiated on its own.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlmodel import select

from ._db_common import _as_day
from .models import (
    SUMMARY_COLUMNS,
    BodyBattery,
    BodyMeasurement,
    Hrv,
    Hydration,
    Readiness,
    RestingHr,
    Sleep,
    Steps,
    Stress,
    Weight,
    Workout,
)

class MetricsMixin:
    """Garmin-sourced metric upserts, the daily-summary reads, readiness,"""

    def upsert_sleep(self, day: str | date, **f: Any) -> None:
        self._upsert(Sleep(day=_as_day(day), **f))

    def upsert_hrv(self, day: str | date, **f: Any) -> None:
        self._upsert(Hrv(day=_as_day(day), **f))

    def upsert_resting_hr(self, day: str | date, **f: Any) -> None:
        self._upsert(RestingHr(day=_as_day(day), **f))

    def upsert_stress(self, day: str | date, **f: Any) -> None:
        self._upsert(Stress(day=_as_day(day), **f))

    def upsert_body_battery(self, day: str | date, **f: Any) -> None:
        self._upsert(BodyBattery(day=_as_day(day), **f))

    def upsert_steps(self, day: str | date, **f: Any) -> None:
        self._upsert(Steps(day=_as_day(day), **f))

    def upsert_weight(self, day: str | date, **f: Any) -> None:
        self._upsert(Weight(day=_as_day(day), **f))

    def upsert_hydration(self, day: str | date, **f: Any) -> None:
        self._upsert(Hydration(day=_as_day(day), **f))

    def upsert_workout(self, activity_id: int, day: str | date, **f: Any) -> None:
        self._upsert(Workout(activity_id=activity_id, day=_as_day(day), **f))

    def daily_summary(self, days: int = 30) -> list[dict[str, Any]]:
        """Rows for the last ``days`` *calendar* days (today inclusive), oldest
        first. Days with no data at all are absent, not padded — callers doing
        time-window math must not assume one row per day."""
        return self._view_rows(
            "SELECT * FROM daily_summary WHERE day >= :cutoff ORDER BY day",
            {"cutoff": self._cutoff(days)},
        )

    def latest_summary(self) -> dict[str, Any] | None:
        rows = self._view_rows(
            "SELECT * FROM daily_summary ORDER BY day DESC LIMIT 1", {}
        )
        return rows[0] if rows else None

    def has_data(self) -> bool:
        """Whether any daily row exists at all (used by the backfill check)."""
        return self.latest_summary() is not None

    def metric_series(self, column: str, days: int = 30) -> list[dict[str, Any]]:
        """Return ``[{day, value}, ...]`` for one column of the summary view,
        restricted to the last ``days`` calendar days."""
        if column not in SUMMARY_COLUMNS:
            raise ValueError(f"Unknown metric column: {column}")
        return self._view_rows(
            f"SELECT day, {column} AS value FROM daily_summary "
            f"WHERE {column} IS NOT NULL AND day >= :cutoff ORDER BY day",
            {"cutoff": self._cutoff(days)},
        )

    def recent_workouts(
        self, days: int = 28, include_duplicates: bool = False
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = (
                select(Workout)
                .where(Workout.day >= self._cutoff(days))
                .order_by(Workout.day.desc(), Workout.activity_id.desc())
            )
            if not include_duplicates:
                stmt = stmt.where(Workout.duplicate_of == None)  # noqa: E711
            rows = s.exec(stmt).all()
        return [r.model_dump() for r in rows]

    def add_hydration_intake(self, ml: int, day: str | date | None = None) -> int:
        """Add ``ml`` to the day's hydration total and return the new total.

        Distinct from ``upsert_hydration``, which *sets* the total — /water 500
        must accumulate across the day, not overwrite earlier glasses.
        """
        day_str = _as_day(day or date.today())
        with self.session() as s:
            row = s.get(Hydration, day_str)
            current = row.intake_ml if row and row.intake_ml is not None else 0
        total = current + int(ml)
        self.upsert_hydration(day_str, intake_ml=total)
        return total

    # ── Readiness ──────────────────────────────────────────────────────────────

    def upsert_readiness(self, day: str | date, **f: Any) -> None:
        self._upsert(Readiness(day=_as_day(day), **f))

    def recent_readiness(self, days: int = 14) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Readiness)
                .where(Readiness.day >= self._cutoff(days))
                .order_by(Readiness.day)
            ).all()
        return [r.model_dump() for r in rows]

    def latest_readiness(self) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.exec(
                select(Readiness).order_by(Readiness.day.desc()).limit(1)
            ).first()
        return row.model_dump() if row else None

    # ── Body measurements ──────────────────────────────────────────────────────

    def upsert_body_measurement(self, day: str | date, **f: Any) -> None:
        self._upsert(BodyMeasurement(day=_as_day(day), **f))

    def recent_body_measurements(self, days: int = 90) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(BodyMeasurement)
                .where(BodyMeasurement.day >= self._cutoff(days))
                .order_by(BodyMeasurement.day)
            ).all()
        return [r.model_dump() for r in rows]

    # ── Hydration reads ────────────────────────────────────────────────────────

    def recent_hydration(self, days: int = 14) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Hydration)
                .where(Hydration.day >= self._cutoff(days))
                .order_by(Hydration.day)
            ).all()
        out = []
        for r in rows:
            pct = None
            if r.intake_ml is not None and r.goal_ml:
                pct = round(100.0 * r.intake_ml / r.goal_ml, 1)
            entry = r.model_dump()
            entry["percent_of_goal"] = pct
            out.append(entry)
        return out
