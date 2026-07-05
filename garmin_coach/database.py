"""Database layer built on SQLModel.

Exposes a small :class:`Database` facade: SQLModel handles the ORM tables while a
hand-written ``daily_summary`` SQL view stitches the metric families together for
convenient reads. Upserts are field-preserving (insert-or-update on primary key,
never overwriting an existing value with ``None``), so re-pulling a day is
idempotent and a partial write can't erase data a fuller write already stored.

Schema evolution: ``create_all`` only creates *missing tables* — it never alters
existing ones, so a model gaining a column would otherwise break inserts against
databases created before the column existed. ``init_schema`` therefore also
upgrades to the Alembic head (revision scripts in ``garmin_coach/alembic``, for
data fixes and anything with intent) and then reconciles columns
(``_migrate_missing_columns``): any nullable model column absent from the live
table is added via ``ALTER TABLE ... ADD COLUMN``, which is additive, lossless,
and idempotent.
"""

from __future__ import annotations

import fcntl
import itertools
import json
import logging
import sqlite3
import time as _time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from .config import settings
from .models import (
    SUMMARY_COLUMNS,
    BodyBattery,
    BodyMeasurement,
    Conversation,
    Feedback,
    HealthEvent,
    Hrv,
    Hydration,
    Meal,
    Plan,
    PlanDetail,
    Profile,
    PullLog,
    Readiness,
    RestingHr,
    Sleep,
    Steps,
    StrengthExercise,
    StrengthSession,
    Stress,
    TrainingPlan,
    TrainingPlanEdit,
    Vital,
    Weight,
    Workout,
    WorkoutLogFlow,
)
from .training_load import estimate_training_load

log = logging.getLogger("garmin_coach.database")

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
    (SELECT COUNT(*)  FROM workout wo WHERE wo.day = d.day AND wo.duplicate_of IS NULL)                       AS workout_count,
    (SELECT COALESCE(SUM(training_load), 0) FROM workout wo WHERE wo.day = d.day AND wo.duplicate_of IS NULL) AS training_load,
    (SELECT COALESCE(SUM(calories), 0) FROM meal me WHERE me.day = d.day)         AS calories_in
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
    UNION SELECT day FROM meal
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


# Synthetic (negative) activity IDs for workouts not synced from Garmin.
# Time-based so they can never collide with Garmin's positive IDs across
# restarts; the counter disambiguates calls in the same millisecond.
_synthetic_seq = itertools.count()


def synthetic_activity_id() -> int:
    return -(int(_time.time() * 1000) * 1000 + next(_synthetic_seq) % 1000)


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
        # Column reconciliation runs BEFORE the Alembic upgrade so revisions
        # can rely on model columns existing (e.g. 0003 backfills columns the
        # reconciler just added).
        self._migrate_missing_columns()
        self._upgrade_to_alembic_head()
        with self.engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            try:
                for stmt in filter(None, (s.strip() for s in _DAILY_SUMMARY_VIEW.split(";"))):
                    conn.exec_driver_sql(stmt)
            except OperationalError as exc:
                # Sibling containers share this SQLite file and each recreate the
                # view at startup; a concurrent DROP/CREATE from another process
                # racing this one is harmless since both run identical code.
                if "already exists" not in str(exc):
                    raise

    def _alembic_config(self) -> AlembicConfig:
        cfg = AlembicConfig()
        cfg.set_main_option(
            "script_location", str(Path(__file__).resolve().parent / "alembic")
        )
        # Share this engine so migrations inherit the 30s busy timeout —
        # essential with four containers upgrading one SQLite file at boot.
        cfg.attributes["engine"] = self.engine
        return cfg

    def _upgrade_to_alembic_head(self) -> None:
        """Apply pending Alembic revisions under a cross-container file lock.

        The lock file lives next to the DB on the shared volume, so exactly
        one container performs each upgrade; the others block briefly, then
        find head already applied (a no-op). Operations are also written
        guarded/idempotent as a second line of defence.
        """
        lock_path = Path(self.path).expanduser().parent / ".migrations.lock"
        with open(lock_path, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                alembic_command.upgrade(self._alembic_config(), "head")
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _migrate_missing_columns(self) -> None:
        """Add model columns missing from existing tables (additive, lossless).

        ``create_all`` skips tables that already exist, so a database created
        before a model gained a column (e.g. the macro fields on ``meal``)
        would keep failing every INSERT that names it. Only nullable columns
        are added — that's the only ALTER SQLite allows without a default, and
        the only migration that can't lose or corrupt existing rows. Runs on
        every startup; already-present columns are simply skipped.
        """
        with self.engine.begin() as conn:
            for table in SQLModel.metadata.tables.values():
                rows = conn.exec_driver_sql(
                    f"PRAGMA table_info('{table.name}')"
                ).fetchall()
                existing = {row[1] for row in rows}
                if not existing:
                    continue  # brand-new table — create_all already built it fully
                for column in table.columns:
                    if column.name in existing:
                        continue
                    if not column.nullable:
                        # SQLite can't ADD a NOT NULL column without a default,
                        # and guessing a backfill risks corrupting data. Leave
                        # it to a manual migration rather than crash every
                        # service at boot.
                        log.error(
                            "Cannot auto-migrate NOT NULL column %s.%s — "
                            "add it manually.", table.name, column.name,
                        )
                        continue
                    coltype = column.type.compile(self.engine.dialect)
                    try:
                        conn.exec_driver_sql(
                            f'ALTER TABLE "{table.name}" '
                            f'ADD COLUMN "{column.name}" {coltype}'
                        )
                    except OperationalError as exc:
                        # Sibling containers race this migration at startup;
                        # losing the race to an identical ALTER is harmless.
                        if "duplicate column" not in str(exc):
                            raise

    # ── Upserts (insert-or-update on primary key, field-preserving) ────────────

    def _upsert(self, instance: SQLModel) -> None:
        """Insert, or update only the non-``None`` incoming fields.

        A plain ``session.merge`` replaces *every* column, so a partial write —
        a Garmin re-pull where one field is missing, or a manual ``log_weight``
        that only carries weight — would null out values an earlier, fuller
        write already stored. ``None`` here always means "no data", never
        "erase", so existing values are kept.
        """
        model = type(instance)
        pk_names = [c.name for c in model.__table__.primary_key.columns]
        with self.session() as s:
            existing = s.get(model, tuple(getattr(instance, n) for n in pk_names))
            if existing is None:
                s.add(instance)
            else:
                for name in model.model_fields:
                    if name in pk_names:
                        continue
                    value = getattr(instance, name)
                    if value is not None:
                        setattr(existing, name, value)
                s.add(existing)
            s.commit()

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

    # ── Summary reads (raw SQL against the view) ───────────────────────────────

    def _view_rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self.session() as s:
            result = s.exec(text(sql).bindparams(**params))
            return [dict(r._mapping) for r in result]

    @staticmethod
    def _cutoff(days: int) -> str:
        """ISO day string ``days`` calendar days back, today inclusive."""
        return (date.today() - timedelta(days=days - 1)).isoformat()

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

    # ── Meals (user-logged, not pulled from Garmin) ─────────────────────────────

    def add_meal(
        self,
        name: str,
        day: str | date | None = None,
        calories: int | None = None,
        protein_g: float | None = None,
        carbs_g: float | None = None,
        fat_g: float | None = None,
        fiber_g: float | None = None,
        sugar_g: float | None = None,
        note: str | None = None,
    ) -> None:
        with self.session() as s:
            s.add(
                Meal(
                    day=_as_day(day or date.today()),
                    name=name,
                    calories=calories,
                    protein_g=protein_g,
                    carbs_g=carbs_g,
                    fat_g=fat_g,
                    fiber_g=fiber_g,
                    sugar_g=sugar_g,
                    note=note,
                )
            )
            s.commit()

    def recent_meals(self, days: int = 7) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Meal).order_by(Meal.day.desc(), Meal.id.desc()).limit(days * 6)
            ).all()
        return [r.model_dump() for r in reversed(rows)]

    # ── Vitals (generic named readings: blood pressure, glucose, SpO2, etc.) ───

    def add_vital(
        self,
        metric: str,
        value: float,
        day: str | date | None = None,
        unit: str | None = None,
        note: str | None = None,
    ) -> None:
        with self.session() as s:
            s.add(
                Vital(
                    day=_as_day(day or date.today()),
                    metric=metric,
                    value=value,
                    unit=unit,
                    note=note,
                )
            )
            s.commit()

    def recent_vitals(
        self, metric: str | None = None, days: int = 30
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(Vital).order_by(Vital.day.desc(), Vital.id.desc())
            if metric:
                stmt = stmt.where(Vital.metric == metric)
            rows = s.exec(stmt.limit(days * 10)).all()
        return [r.model_dump() for r in reversed(rows)]

    # ── Health events (structured Telegram-captured logs) ─────────────────────

    @staticmethod
    def _event_dict(row: HealthEvent) -> dict[str, Any]:
        out = row.model_dump()
        raw = out.pop("payload_json", None)
        try:
            out["payload"] = json.loads(raw) if raw else None
        except ValueError:
            out["payload"] = raw  # a corrupt payload must not break event reads
        return out

    def add_health_event(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        day: str | date | None = None,
        source: str = "telegram",
    ) -> dict[str, Any]:
        with self.session() as s:
            row = HealthEvent(
                kind=kind,
                source=source,
                day=_as_day(day or date.today()),
                payload_json=(
                    json.dumps(payload, ensure_ascii=False)
                    if payload is not None else None
                ),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._event_dict(row)

    def recent_health_events(
        self, days: int = 7, kind: str | None = None
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = (
                select(HealthEvent)
                .where(HealthEvent.day >= self._cutoff(days))
                .order_by(HealthEvent.day, HealthEvent.id)
            )
            if kind:
                stmt = stmt.where(HealthEvent.kind == kind)
            rows = s.exec(stmt).all()
        return [self._event_dict(r) for r in rows]

    def health_events_for_day(self, day: str | date) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(HealthEvent)
                .where(HealthEvent.day == _as_day(day))
                .order_by(HealthEvent.id)
            ).all()
        return [self._event_dict(r) for r in rows]

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

    def save_plan(
        self, day: str | date, plan: str, details: dict[str, Any] | None = None
    ) -> None:
        from datetime import timezone
        with self.session() as s:
            s.merge(Plan(day=_as_day(day), ts=datetime.now(timezone.utc), plan=plan))
            if details is not None:
                s.merge(
                    PlanDetail(
                        day=_as_day(day),
                        ts=datetime.now(timezone.utc),
                        data=json.dumps(details, ensure_ascii=False),
                    )
                )
            s.commit()

    def last_plan(self) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.exec(select(Plan).order_by(Plan.day.desc()).limit(1)).first()
            if row is None:
                return None
            out: dict[str, Any] = {"day": row.day, "plan": row.plan}
            detail = s.get(PlanDetail, row.day)
        if detail is not None:
            try:
                out["details"] = json.loads(detail.data)
            except ValueError:
                pass  # a corrupt details row must not break plan reads
        return out

    # ── Pull log (which days Garmin has been pulled for) ───────────────────────

    def record_pull(self, day: str | date, status: dict[str, str]) -> None:
        self._upsert(PullLog(day=_as_day(day), status=json.dumps(status)))

    def pulled_days(self, start: str | date, end: str | date) -> set[str]:
        """Days in ``[start, end]`` that have a successful pull recorded."""
        with self.session() as s:
            rows = s.exec(
                select(PullLog.day)
                .where(PullLog.day >= _as_day(start), PullLog.day <= _as_day(end))
            ).all()
        return set(rows)

    def last_pull(self) -> dict[str, Any] | None:
        """The most recent Garmin pull: the day it covered, when it ran, and
        its per-metric results. ``ts`` is updated on every re-pull of a day,
        so this reflects actual sync recency, not just the newest day."""
        with self.session() as s:
            row = s.exec(select(PullLog).order_by(PullLog.ts.desc()).limit(1)).first()
        if row is None:
            return None
        try:
            status = json.loads(row.status) if row.status else None
        except ValueError:
            status = row.status
        return {"day": row.day, "ts": row.ts, "status": status}

    # ── Profile / goals (single row, id=1) ─────────────────────────────────────

    def get_profile(self) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(Profile, 1)
        return row.model_dump() if row else None

    def set_profile(self, replace: bool = False, **fields: Any) -> dict[str, Any]:
        """Update profile fields. Partial by default (None leaves a field
        alone); ``replace=True`` rewrites the whole profile from ``fields``."""
        with self.session() as s:
            row = s.get(Profile, 1)
            if row is None or replace:
                if row is not None:
                    s.delete(row)
                    s.flush()
                row = Profile(id=1, **{k: v for k, v in fields.items() if v is not None})
            else:
                for key, value in fields.items():
                    if value is not None:
                        setattr(row, key, value)
                row.updated_at = datetime.now()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.model_dump()

    # ── Strength sessions ──────────────────────────────────────────────────────

    def _strength_rpe(self, exercises: list[dict[str, Any]]) -> float | None:
        rpes = [e.get("rpe") for e in exercises if e.get("rpe") is not None]
        return sum(rpes) / len(rpes) if rpes else None

    @staticmethod
    def _prepare_exercise(ex: dict[str, Any]) -> dict[str, Any]:
        """Normalise one incoming exercise dict for storage.

        Per-set data (``actual_sets``) is kept verbatim as JSON and also
        collapsed into the aggregate columns (top weight, average reps/RPE,
        set count) so single-row history queries keep working. A ``completed:
        False`` without an explicit status becomes status="skipped"."""
        ex = dict(ex)
        actual_sets = ex.pop("actual_sets", None)
        if actual_sets:
            ex["set_details"] = json.dumps(actual_sets, ensure_ascii=False)
            ex.setdefault("sets", len(actual_sets))
            reps = [s.get("reps") for s in actual_sets if s.get("reps") is not None]
            if reps and ex.get("reps") is None:
                ex["reps"] = round(sum(reps) / len(reps))
            weights = [s.get("weight_kg") for s in actual_sets if s.get("weight_kg") is not None]
            if weights and ex.get("weight_kg") is None:
                ex["weight_kg"] = max(weights)
            rpes = [s.get("rpe") for s in actual_sets if s.get("rpe") is not None]
            if rpes and ex.get("rpe") is None:
                ex["rpe"] = round(sum(rpes) / len(rpes), 1)
        if ex.get("status") is None and ex.get("completed") is False:
            ex["status"] = "skipped"
        return ex

    @staticmethod
    def _exercise_dict(row: StrengthExercise) -> dict[str, Any]:
        out = row.model_dump()
        detail = out.pop("set_details", None)
        if detail:
            try:
                out["actual_sets"] = json.loads(detail)
            except ValueError:
                out["actual_sets"] = None
        else:
            out["actual_sets"] = None
        return out

    def add_strength_session(
        self,
        day: str | date,
        exercises: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Create a strength session plus its exercises, mirrored into a
        Workout row (estimated training load) for history and load math."""
        prepared = [self._prepare_exercise(ex) for ex in (exercises or [])]
        day_str = _as_day(day)
        activity_id = synthetic_activity_id()
        with self.session() as s:
            session_row = StrengthSession(day=day_str, activity_id=activity_id, **fields)
            s.add(session_row)
            s.flush()
            for ex in prepared:
                s.add(StrengthExercise(session_id=session_row.id, **ex))
            s.commit()
            s.refresh(session_row)
            session_id = session_row.id

        load = estimate_training_load(
            "strength_training",
            fields.get("duration_s"),
            avg_hr=fields.get("avg_hr"),
            rpe=self._strength_rpe(prepared),
        )
        self.upsert_workout(
            activity_id,
            day_str,
            name=fields.get("session_name") or "Strength session",
            type="strength_training",
            duration_s=fields.get("duration_s"),
            calories=fields.get("calories"),
            avg_hr=fields.get("avg_hr"),
            max_hr=fields.get("max_hr"),
            training_load=load,
            source="manual",
            load_source="estimated" if load is not None else None,
        )
        return self.get_strength_session(session_id)

    def get_strength_session(self, session_id: int) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(StrengthSession, session_id)
            if row is None:
                return None
            exercises = s.exec(
                select(StrengthExercise)
                .where(StrengthExercise.session_id == session_id)
                .order_by(StrengthExercise.id)
            ).all()
            out = row.model_dump()
            out["exercises"] = [self._exercise_dict(e) for e in exercises]
            return out

    def recent_strength_sessions(self, days: int = 30) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(StrengthSession)
                .where(StrengthSession.day >= self._cutoff(days))
                .order_by(StrengthSession.day, StrengthSession.id)
            ).all()
            ids = [r.id for r in rows]
        return [self.get_strength_session(i) for i in ids]

    def update_strength_session(
        self,
        session_id: int,
        exercises: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Partial update; passing ``exercises`` replaces the exercise list."""
        with self.session() as s:
            row = s.get(StrengthSession, session_id)
            if row is None:
                return None
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            if exercises is not None:
                for old in s.exec(
                    select(StrengthExercise).where(StrengthExercise.session_id == session_id)
                ).all():
                    s.delete(old)
                for ex in exercises:
                    s.add(StrengthExercise(session_id=session_id, **self._prepare_exercise(ex)))
            s.add(row)
            s.commit()
            activity_id, day = row.activity_id, row.day

        updated = self.get_strength_session(session_id)
        if activity_id is not None:
            load = estimate_training_load(
                "strength_training",
                updated.get("duration_s"),
                avg_hr=updated.get("avg_hr"),
                rpe=self._strength_rpe(updated["exercises"]),
            )
            self.upsert_workout(
                activity_id,
                day,
                name=updated.get("session_name"),
                duration_s=updated.get("duration_s"),
                calories=updated.get("calories"),
                avg_hr=updated.get("avg_hr"),
                max_hr=updated.get("max_hr"),
                training_load=load,
                load_source="estimated" if load is not None else None,
            )
        return updated

    def delete_strength_session(self, session_id: int) -> bool:
        with self.session() as s:
            row = s.get(StrengthSession, session_id)
            if row is None:
                return False
            activity_id = row.activity_id
            for ex in s.exec(
                select(StrengthExercise).where(StrengthExercise.session_id == session_id)
            ).all():
                s.delete(ex)
            s.delete(row)
            if activity_id is not None:
                workout = s.get(Workout, activity_id)
                if workout is not None:
                    s.delete(workout)
            s.commit()
        return True

    def exercise_history(
        self, exercise_name: str, days: int = 180, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Past occurrences of one exercise (newest first) with volume and
        best-set info — the raw material for progressive-overload checks."""
        with self.session() as s:
            rows = s.exec(
                select(StrengthExercise, StrengthSession)
                .where(StrengthExercise.session_id == StrengthSession.id)
                .where(StrengthSession.day >= self._cutoff(days))
                .where(StrengthExercise.exercise_name.ilike(exercise_name))
                .order_by(StrengthSession.day.desc(), StrengthExercise.id.desc())
                .limit(limit)
            ).all()
        history = []
        for exercise, session in rows:
            detail = self._exercise_dict(exercise)
            actual_sets = detail.get("actual_sets") or []
            per_set = [
                (s.get("reps"), s.get("weight_kg"))
                for s in actual_sets
                if s.get("reps") is not None and s.get("weight_kg") is not None
            ]
            if per_set:
                volume = round(sum(r * w for r, w in per_set), 1)
                best = max(w for _r, w in per_set)
            else:
                volume = (
                    round(exercise.sets * exercise.reps * exercise.weight_kg, 1)
                    if exercise.sets and exercise.reps and exercise.weight_kg
                    else None
                )
                best = exercise.weight_kg
            history.append(
                {
                    "date": session.day,
                    "session_id": session.id,
                    "session_name": session.session_name,
                    "gym": session.gym,
                    "machine": exercise.machine,
                    "planned_sets": exercise.planned_sets,
                    "planned_reps": exercise.planned_reps,
                    "planned_weight_kg": exercise.planned_weight_kg,
                    "sets": exercise.sets,
                    "reps": exercise.reps,
                    "weight_kg": exercise.weight_kg,
                    "actual_sets": actual_sets or None,
                    "estimated_volume_kg": volume,
                    "best_set_weight_kg": best,
                    "rpe": exercise.rpe,
                    "rir": exercise.rir,
                    "status": exercise.status,
                    "substitute_exercise": exercise.substitute_exercise,
                    "completed": exercise.completed,
                    "pain_note": exercise.pain_note,
                }
            )
        return history

    def recently_trained_exercises(self, days: int = 120) -> list[str]:
        """Distinct exercise names trained in the window, most recent first."""
        with self.session() as s:
            rows = s.exec(
                select(StrengthExercise.exercise_name, StrengthSession.day)
                .where(StrengthExercise.session_id == StrengthSession.id)
                .where(StrengthSession.day >= self._cutoff(days))
                .order_by(StrengthSession.day.desc())
            ).all()
        seen: list[str] = []
        for name, _day in rows:
            if name.lower() not in {n.lower() for n in seen}:
                seen.append(name)
        return seen

    # ── Training plans (planned workouts + adherence) ──────────────────────────

    def create_training_plan(self, day: str | date, **fields: Any) -> dict[str, Any]:
        exercises = fields.pop("exercises", None)
        if isinstance(exercises, (list, dict)):
            exercises = json.dumps(exercises, ensure_ascii=False)
        with self.session() as s:
            row = TrainingPlan(day=_as_day(day), exercises=exercises, **fields)
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._plan_dict(row)

    @staticmethod
    def _plan_dict(row: TrainingPlan) -> dict[str, Any]:
        out = row.model_dump()
        if out.get("exercises"):
            try:
                out["exercises"] = json.loads(out["exercises"])
            except ValueError:
                pass
        return out

    def get_training_plans(
        self, days: int = 14, status: str | None = None
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = (
                select(TrainingPlan)
                .where(TrainingPlan.day >= self._cutoff(days))
                .order_by(TrainingPlan.day, TrainingPlan.id)
            )
            if status:
                stmt = stmt.where(TrainingPlan.status == status)
            rows = s.exec(stmt).all()
        return [self._plan_dict(r) for r in rows]

    def get_training_plans_for_day(self, day: str | date) -> list[dict[str, Any]]:
        day_str = _as_day(day)
        with self.session() as s:
            rows = s.exec(
                select(TrainingPlan)
                .where(TrainingPlan.day == day_str)
                .order_by(TrainingPlan.id)
            ).all()
        return [self._plan_dict(r) for r in rows]

    def get_today_training_plans(self) -> list[dict[str, Any]]:
        return self.get_training_plans_for_day(date.today())

    def get_training_plan(self, plan_id: int) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(TrainingPlan, plan_id)
            return self._plan_dict(row) if row is not None else None

    _PLAN_STATUSES = {"planned", "done", "skipped", "partially_done"}

    def update_training_plan(self, plan_id: int, **fields: Any) -> dict[str, Any] | None:
        """Partial update. Only non-``None`` fields change; a changed field is
        recorded in ``training_plan_edit`` and ``updated_at`` is bumped. Raises
        ``ValueError`` for an unrecognised ``status``."""
        status = fields.get("status")
        if status is not None and status not in self._PLAN_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(self._PLAN_STATUSES)}, got {status!r}"
            )
        exercises = fields.pop("exercises", None)
        if isinstance(exercises, (list, dict)):
            fields["exercises"] = json.dumps(exercises, ensure_ascii=False)
        elif exercises is not None:
            fields["exercises"] = exercises

        with self.session() as s:
            row = s.get(TrainingPlan, plan_id)
            if row is None:
                return None
            changes: dict[str, Any] = {}
            for key, value in fields.items():
                if value is None:
                    continue
                old = getattr(row, key, None)
                if old != value:
                    changes[key] = {"old": old, "new": value}
                setattr(row, key, value)
            if changes:
                row.updated_at = datetime.now(timezone.utc)
                s.add(
                    TrainingPlanEdit(
                        plan_id=plan_id,
                        changes_json=json.dumps(changes, ensure_ascii=False, default=str),
                    )
                )
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._plan_dict(row)

    def training_plan_history(self, plan_id: int) -> list[dict[str, Any]]:
        """Audit trail of changes applied via update_training_plan, oldest first."""
        with self.session() as s:
            rows = s.exec(
                select(TrainingPlanEdit)
                .where(TrainingPlanEdit.plan_id == plan_id)
                .order_by(TrainingPlanEdit.id)
            ).all()
        out = []
        for r in rows:
            entry = r.model_dump()
            try:
                entry["changes"] = json.loads(entry.pop("changes_json"))
            except ValueError:
                entry["changes"] = {}
            out.append(entry)
        return out

    # ── Meals: partial update / delete ─────────────────────────────────────────

    def update_meal(self, meal_id: int, **fields: Any) -> str | None:
        """Apply non-None fields; returns the meal's day, or None if missing."""
        with self.session() as s:
            row = s.get(Meal, meal_id)
            if row is None:
                return None
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            s.add(row)
            s.commit()
            return row.day

    def delete_meal(self, meal_id: int) -> str | None:
        with self.session() as s:
            row = s.get(Meal, meal_id)
            if row is None:
                return None
            day = row.day
            s.delete(row)
            s.commit()
            return day

    def meals_for_day(self, day: str | date) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Meal).where(Meal.day == _as_day(day)).order_by(Meal.id)
            ).all()
        return [r.model_dump() for r in rows]

    # ── Workouts: partial update / delete ──────────────────────────────────────

    def update_workout(self, activity_id: int, **fields: Any) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(Workout, activity_id)
            if row is None:
                return None
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.model_dump()

    def delete_workout(self, activity_id: int) -> str | None:
        with self.session() as s:
            row = s.get(Workout, activity_id)
            if row is None:
                return None
            day = row.day
            s.delete(row)
            s.commit()
            return day

    def workouts_for_day(self, day: str | date) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Workout)
                .where(Workout.day == _as_day(day))
                .where(Workout.duplicate_of == None)  # noqa: E711
                .order_by(Workout.activity_id)
            ).all()
        return [r.model_dump() for r in rows]

    # ── Nutrition summary ──────────────────────────────────────────────────────

    def nutrition_summary(
        self, days: int = 7, day: str | date | None = None
    ) -> list[dict[str, Any]]:
        """Per-day nutrition totals vs. profile targets, newest day first.
        Meals missing macros simply contribute nothing to those totals."""
        if day is not None:
            day_list = [_as_day(day)]
            with self.session() as s:
                meal_rows = s.exec(select(Meal).where(Meal.day == day_list[0])).all()
        else:
            cutoff = self._cutoff(days)
            with self.session() as s:
                meal_rows = s.exec(
                    select(Meal).where(Meal.day >= cutoff).order_by(Meal.day, Meal.id)
                ).all()
            day_list = sorted({m.day for m in meal_rows}, reverse=True)

        profile = self.get_profile() or {}
        calorie_target = profile.get("calorie_target")
        protein_target = profile.get("protein_target_g")

        by_day: dict[str, list[Meal]] = {}
        for meal in meal_rows:
            by_day.setdefault(meal.day, []).append(meal)

        def total(meals: list[Meal], field: str) -> float | None:
            values = [getattr(m, field) for m in meals if getattr(m, field) is not None]
            return round(sum(values), 1) if values else None

        out = []
        for d in day_list:
            meals = by_day.get(d, [])
            calories = total(meals, "calories")
            protein = total(meals, "protein_g")
            out.append(
                {
                    "day": d,
                    "total_calories": calories,
                    "total_protein_g": protein,
                    "total_carbs_g": total(meals, "carbs_g"),
                    "total_fat_g": total(meals, "fat_g"),
                    "total_fiber_g": total(meals, "fiber_g"),
                    "total_sugar_g": total(meals, "sugar_g"),
                    "meal_count": len(meals),
                    "calorie_target": calorie_target,
                    "protein_target_g": protein_target,
                    "calories_remaining": (
                        round(calorie_target - (calories or 0), 1)
                        if calorie_target is not None else None
                    ),
                    "protein_remaining_g": (
                        round(protein_target - (protein or 0), 1)
                        if protein_target is not None else None
                    ),
                    "meals": [m.model_dump() for m in meals],
                }
            )
        return out

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

    # ── Workout deduplication ──────────────────────────────────────────────────

    # garmin_merged ranks above plain garmin: it's a deliberately curated
    # canonical session, so a same-day Apple/manual import that looks like a
    # duplicate must never win against it and hide the merged summary.
    _DEFAULT_SOURCE_PRIORITY = ("garmin_merged", "garmin", "apple", "manual")

    def _source_priority(self) -> list[str]:
        profile = self.get_profile() or {}
        raw = profile.get("activity_source_priority") or ""
        order = [p.strip() for p in raw.split(",") if p.strip()]
        return order or list(self._DEFAULT_SOURCE_PRIORITY)

    @staticmethod
    def _workout_source(w: dict[str, Any]) -> str:
        if w.get("source"):
            return w["source"]
        return "garmin" if w["activity_id"] > 0 else "manual"

    @staticmethod
    def _close(a: float | None, b: float | None, tolerance: float) -> bool | None:
        """True/False when both present, None when incomparable."""
        if a is None or b is None:
            return None
        biggest = max(abs(a), abs(b))
        if biggest == 0:
            return True
        return abs(a - b) / biggest <= tolerance

    @classmethod
    def _looks_duplicate(cls, a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Same-day activities that look like one physical workout recorded
        twice (Garmin + Apple/manual import). Requires comparable duration,
        and rejects on any clearly-different measurable."""
        duration = cls._close(a.get("duration_s"), b.get("duration_s"), 0.15)
        if duration is not True:
            return False
        distance = cls._close(a.get("distance_m"), b.get("distance_m"), 0.15)
        if distance is False:
            return False
        calories = cls._close(
            float(a["calories"]) if a.get("calories") is not None else None,
            float(b["calories"]) if b.get("calories") is not None else None,
            0.25,
        )
        if calories is False:
            return False
        return True

    def find_duplicate_workouts(self, days: int = 60) -> list[list[dict[str, Any]]]:
        """Groups of same-day activities that look like one workout recorded
        by multiple sources. Detection only — nothing is modified."""
        workouts = self.recent_workouts(days=days, include_duplicates=False)
        by_day: dict[str, list[dict[str, Any]]] = {}
        for w in workouts:
            by_day.setdefault(w["day"], []).append(w)

        groups: list[list[dict[str, Any]]] = []
        for day_workouts in by_day.values():
            remaining = list(day_workouts)
            while remaining:
                seed = remaining.pop(0)
                group = [seed]
                still = []
                for other in remaining:
                    if self._looks_duplicate(seed, other):
                        group.append(other)
                    else:
                        still.append(other)
                remaining = still
                if len(group) > 1:
                    groups.append(group)
        return groups

    def dedupe_workouts(self, days: int = 60) -> dict[str, Any]:
        """Mark duplicates (soft delete): in each detected group the workout
        from the highest-priority source is kept, the rest get
        ``duplicate_of = <kept activity_id>``. Reversible via update_workout."""
        priority = self._source_priority()

        def rank(w: dict[str, Any]) -> tuple:
            source = self._workout_source(w)
            idx = priority.index(source) if source in priority else len(priority)
            has_real_load = 0 if w.get("load_source") == "garmin" else 1
            return (idx, has_real_load, -w["activity_id"])

        groups = self.find_duplicate_workouts(days=days)
        marked = []
        with self.session() as s:
            for group in groups:
                keeper, *dupes = sorted(group, key=rank)
                for dupe in dupes:
                    row = s.get(Workout, dupe["activity_id"])
                    row.duplicate_of = keeper["activity_id"]
                    s.add(row)
                    marked.append(
                        {
                            "marked_duplicate": dupe["activity_id"],
                            "kept": keeper["activity_id"],
                            "day": dupe["day"],
                            "name": dupe.get("name"),
                        }
                    )
            s.commit()
        return {"groups_found": len(groups), "marked": marked, "priority": priority}

    # ── Garmin strength-fragment merging ────────────────────────────────────────

    def _plan_strength_merge(
        self,
        day_str: str,
        group: list[dict[str, Any]],
        existing_fragment_sets: dict[int, set[int]],
        dry_run: bool,
    ) -> dict[str, Any]:
        """Compute (and, unless ``dry_run``, apply) the merge of one group of
        same-day fragments. ``existing_fragment_sets`` maps a previously
        merged row's activity_id to the fragment ids it currently covers —
        matched by *overlap* rather than exact equality, so a merge stays
        idempotent even if a delayed Garmin sync adds one more fragment to an
        already-merged session on a later call.
        """
        from .strength_merge import weighted_avg_hr

        before_load = round(sum(f.get("training_load") or 0 for f in group), 1)
        total_duration = sum(f.get("duration_s") or 0 for f in group) or None
        total_calories = round(sum(f.get("calories") or 0 for f in group)) or None
        avg_hr = weighted_avg_hr(group)
        max_hr = max(
            (f["max_hr"] for f in group if f.get("max_hr") is not None), default=None
        )
        load_after = estimate_training_load("strength_training", total_duration, avg_hr=avg_hr)
        if load_after is None:
            load_after = before_load
        fragment_ids = sorted(f["activity_id"] for f in group)

        if dry_run:
            return {
                "merged": False, "would_merge": True,
                "fragment_ids": fragment_ids,
                "total_duration_s": total_duration,
                "total_calories": total_calories,
                "avg_hr": avg_hr,
                "max_hr": max_hr,
                "training_load_before": before_load,
                "training_load_after": load_after,
            }

        fragment_id_set = set(fragment_ids)
        merged_id = next(
            (
                activity_id for activity_id, ids in existing_fragment_sets.items()
                if ids & fragment_id_set
            ),
            None,
        )
        if merged_id is None:
            merged_id = synthetic_activity_id()

        self.upsert_workout(
            merged_id, day_str,
            name="Merged Garmin strength session",
            type="strength_training",
            duration_s=total_duration,
            calories=total_calories,
            avg_hr=avg_hr,
            max_hr=max_hr,
            training_load=load_after,
            source="garmin_merged",
            load_source="estimated",
            meta_json=json.dumps({"fragment_ids": fragment_ids}),
        )
        with self.session() as s:
            for fragment_id in fragment_ids:
                fragment = s.get(Workout, fragment_id)
                if fragment is not None:
                    fragment.duplicate_of = merged_id
                    s.add(fragment)
            s.commit()

        return {
            "merged": True, "merged_activity_id": merged_id,
            "fragment_ids": fragment_ids,
            "total_duration_s": total_duration,
            "total_calories": total_calories,
            "avg_hr": avg_hr,
            "max_hr": max_hr,
            "training_load_before": before_load,
            "training_load_after": load_after,
        }

    def merge_garmin_strength_fragments(
        self,
        day: str | date,
        dry_run: bool = False,
        min_fragments: int = 2,
        max_gap_minutes: float = 90.0,
    ) -> dict[str, Any]:
        """Merge same-day Garmin strength-training fragments into one
        canonical workout so training load/recovery math isn't inflated by
        a session the watch recorded as several short activities.

        Only ``source="garmin"`` activities of a strength-like type are
        considered — manual logs and already-merged rows are untouched.
        Originals are never deleted: they're marked ``duplicate_of`` the
        merged row (the same mechanism ``dedupe_workouts`` uses), so
        summaries/training load/workout counts see the session once while
        the raw Garmin rows stay in the database. Re-running for the same
        day (dry_run or not) is idempotent — it recomputes and updates the
        existing merged row(s) instead of creating new ones. If more than
        one group of fragments qualifies (e.g. two separate gym visits the
        same day), every qualifying group is merged: the largest is
        reported at the top level and the rest under ``other_merges`` — so
        a smaller second session is never left permanently double-counted.
        """
        from .strength_merge import group_fragments, is_strength_like

        day_str = _as_day(day)
        with self.session() as s:
            # Not filtered by duplicate_of: a fragment already merged (by an
            # earlier call, for idempotency) must still be found so it can be
            # regrouped and re-matched to its existing merged row.
            rows = s.exec(
                select(Workout)
                .where(Workout.day == day_str)
                .where(Workout.source == "garmin")
            ).all()
            fragments = [r.model_dump() for r in rows if is_strength_like(r.type)]

        if len(fragments) < max(1, min_fragments):
            return {
                "day": day_str, "dry_run": dry_run, "merged": False,
                "reason": (
                    f"found {len(fragments)} strength fragment(s) — need at "
                    f"least {min_fragments} to merge"
                ),
                "fragment_ids": sorted(f["activity_id"] for f in fragments),
            }

        groups = group_fragments(fragments, max_gap_minutes=max_gap_minutes)
        mergeable = sorted(
            (g for g in groups if len(g) >= min_fragments), key=len, reverse=True
        )
        if not mergeable:
            return {
                "day": day_str, "dry_run": dry_run, "merged": False,
                "reason": "fragments are too far apart in time to be one session",
                "groups": [sorted(f["activity_id"] for f in g) for g in groups],
            }

        existing_fragment_sets: dict[int, set[int]] = {}
        if not dry_run:
            with self.session() as s:
                existing_merged = s.exec(
                    select(Workout).where(
                        Workout.day == day_str, Workout.source == "garmin_merged"
                    )
                ).all()
            for existing in existing_merged:
                try:
                    ids = json.loads(existing.meta_json) if existing.meta_json else {}
                except ValueError:
                    ids = {}
                existing_fragment_sets[existing.activity_id] = set(ids.get("fragment_ids", []))

        merges = [
            self._plan_strength_merge(day_str, group, existing_fragment_sets, dry_run)
            for group in mergeable
        ]
        primary, *others = merges
        result = {"day": day_str, "dry_run": dry_run, **primary}
        if others:
            result["other_merges"] = others
        return result

    # ── Backup ─────────────────────────────────────────────────────────────────

    def backup_to(self, dest: str | Path) -> None:
        """Copy the live database to ``dest`` with SQLite's online backup API
        (safe while other containers are writing, unlike a file copy)."""
        dest = Path(dest).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(self.path)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()


def as_json(obj: Any) -> str:
    """Serialise DB rows to pretty JSON (datetimes rendered as ISO strings)."""
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)
