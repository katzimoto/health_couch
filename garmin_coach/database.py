"""Database layer built on SQLModel.

Exposes a small :class:`Database` facade: SQLModel handles the ORM tables while a
hand-written ``daily_summary`` SQL view stitches the metric families together for
convenient reads. Upserts use ``session.merge`` (insert-or-update on primary
key), so re-pulling a day is idempotent.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from .config import settings
from .models import (
    SUMMARY_COLUMNS,
    BodyBattery,
    Conversation,
    Feedback,
    Hrv,
    Hydration,
    Plan,
    RestingHr,
    Sleep,
    Steps,
    Stress,
    Weight,
    Workout,
)

# ── The daily_summary view ──────────────────────────────────────────────────────
# SQLModel/SQLAlchemy does not model views, so we manage this DDL by hand and
# (re)create it every startup to stay in sync with the tables above.
_DAILY_SUMMARY_VIEW = """
DROP VIEW IF EXISTS daily_summary;
CREATE VIEW daily_summary AS
SELECT
    d.day                                    AS day,
    sl.score                                 AS sleep_score,
    ROUND(sl.total_seconds / 3600.0, 2)      AS sleep_hours,
    COALESCE(rhr.resting_hr, sl.resting_hr)  AS resting_hr,
    h.last_night_avg                         AS hrv,
    h.status                                 AS hrv_status,
    st.avg_stress                            AS avg_stress,
    bb.high                                  AS body_battery_high,
    bb.low                                   AS body_battery_low,
    sp.steps                                 AS steps,
    w.weight_kg                              AS weight_kg,
    w.body_fat                               AS body_fat,
    hy.intake_ml                             AS hydration_ml,
    (SELECT COUNT(*)  FROM workout wo WHERE wo.day = d.day)                       AS workout_count,
    (SELECT COALESCE(SUM(training_load), 0) FROM workout wo WHERE wo.day = d.day) AS training_load
FROM (
    SELECT day FROM sleep
    UNION SELECT day FROM resting_hr
    UNION SELECT day FROM hrv
    UNION SELECT day FROM stress
    UNION SELECT day FROM body_battery
    UNION SELECT day FROM steps
    UNION SELECT day FROM weight
    UNION SELECT day FROM hydration
    UNION SELECT day FROM workout
) d
LEFT JOIN sleep         sl  ON sl.day  = d.day
LEFT JOIN resting_hr    rhr ON rhr.day = d.day
LEFT JOIN hrv           h   ON h.day   = d.day
LEFT JOIN stress        st  ON st.day  = d.day
LEFT JOIN body_battery  bb  ON bb.day  = d.day
LEFT JOIN steps         sp  ON sp.day  = d.day
LEFT JOIN weight        w   ON w.day   = d.day
LEFT JOIN hydration     hy  ON hy.day  = d.day
ORDER BY d.day;
"""


def _as_day(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


class Database:
    """SQLModel-backed facade over the health database."""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or settings.db_path
        Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        self.init_schema()

    def session(self) -> Session:
        return Session(self.engine)

    def init_schema(self) -> None:
        SQLModel.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            for stmt in filter(None, (s.strip() for s in _DAILY_SUMMARY_VIEW.split(";"))):
                conn.exec_driver_sql(stmt)

    # ── Upserts (insert-or-update on primary key) ──────────────────────────────

    def _merge(self, instance: SQLModel) -> None:
        with self.session() as s:
            s.merge(instance)
            s.commit()

    def upsert_sleep(self, day: str | date, **f: Any) -> None:
        self._merge(Sleep(day=_as_day(day), **f))

    def upsert_hrv(self, day: str | date, **f: Any) -> None:
        self._merge(Hrv(day=_as_day(day), **f))

    def upsert_resting_hr(self, day: str | date, **f: Any) -> None:
        self._merge(RestingHr(day=_as_day(day), **f))

    def upsert_stress(self, day: str | date, **f: Any) -> None:
        self._merge(Stress(day=_as_day(day), **f))

    def upsert_body_battery(self, day: str | date, **f: Any) -> None:
        self._merge(BodyBattery(day=_as_day(day), **f))

    def upsert_steps(self, day: str | date, **f: Any) -> None:
        self._merge(Steps(day=_as_day(day), **f))

    def upsert_weight(self, day: str | date, **f: Any) -> None:
        self._merge(Weight(day=_as_day(day), **f))

    def upsert_hydration(self, day: str | date, **f: Any) -> None:
        self._merge(Hydration(day=_as_day(day), **f))

    def upsert_workout(self, activity_id: int, day: str | date, **f: Any) -> None:
        self._merge(Workout(activity_id=activity_id, day=_as_day(day), **f))

    # ── Summary reads (raw SQL against the view) ───────────────────────────────

    def _view_rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self.session() as s:
            result = s.exec(text(sql).bindparams(**params))
            return [dict(r._mapping) for r in result]

    def daily_summary(self, days: int = 30) -> list[dict[str, Any]]:
        rows = self._view_rows(
            "SELECT * FROM daily_summary ORDER BY day DESC LIMIT :n", {"n": days}
        )
        return list(reversed(rows))

    def latest_summary(self) -> dict[str, Any] | None:
        rows = self._view_rows(
            "SELECT * FROM daily_summary ORDER BY day DESC LIMIT 1", {}
        )
        return rows[0] if rows else None

    def metric_series(self, column: str, days: int = 30) -> list[dict[str, Any]]:
        """Return ``[{day, value}, ...]`` for one column of the summary view."""
        if column not in SUMMARY_COLUMNS:
            raise ValueError(f"Unknown metric column: {column}")
        rows = self._view_rows(
            f"SELECT day, {column} AS value FROM daily_summary "
            f"WHERE {column} IS NOT NULL ORDER BY day DESC LIMIT :n",
            {"n": days},
        )
        return list(reversed(rows))

    def recent_workouts(self, days: int = 28) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Workout)
                .order_by(Workout.day.desc(), Workout.activity_id.desc())
                .limit(days * 3)
            ).all()
        return [r.model_dump() for r in rows]

    # ── Conversation memory ────────────────────────────────────────────────────

    def add_message(self, role: str, content: str) -> None:
        with self.session() as s:
            s.add(Conversation(role=role, content=content))
            s.commit()

    def recent_messages(self, limit: int = 20) -> list[dict[str, str]]:
        """Return the last ``limit`` messages in chronological order."""
        with self.session() as s:
            rows = s.exec(
                select(Conversation).order_by(Conversation.id.desc()).limit(limit)
            ).all()
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]

    def add_feedback(self, note: str, day: str | date | None = None) -> None:
        with self.session() as s:
            s.add(Feedback(day=_as_day(day or date.today()), note=note))
            s.commit()

    def recent_feedback(self, days: int = 7) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Feedback).order_by(Feedback.id.desc()).limit(days * 4)
            ).all()
        return [{"day": r.day, "note": r.note} for r in reversed(rows)]

    def save_plan(self, day: str | date, plan: str) -> None:
        from datetime import timezone
        with self.session() as s:
            s.merge(Plan(day=_as_day(day), ts=datetime.now(timezone.utc), plan=plan))
            s.commit()

    def last_plan(self) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.exec(select(Plan).order_by(Plan.day.desc()).limit(1)).first()
        return {"day": row.day, "plan": row.plan} if row else None


def as_json(obj: Any) -> str:
    """Serialise DB rows to pretty JSON (datetimes rendered as ISO strings)."""
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)
