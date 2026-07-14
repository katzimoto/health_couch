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
import json
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine

# ``synthetic_activity_id`` lives in ``_db_common`` and is re-exported here so
# ``from garmin_coach.database import synthetic_activity_id`` (used by
# mcp_server) keeps working after the domain split.
from garmin_coach.storage._db_common import synthetic_activity_id as synthetic_activity_id
from garmin_coach.storage._journal_repo import JournalMixin
from garmin_coach.storage._metrics_repo import MetricsMixin
from garmin_coach.storage._nutrition_repo import NutritionMixin
from garmin_coach.storage._profile_repo import ProfileMixin
from garmin_coach.storage._strength_repo import StrengthMixin
from garmin_coach.storage._training_repo import TrainingPlanMixin
from garmin_coach.storage._workout_merge_repo import WorkoutMergeMixin
from garmin_coach.config import settings

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


class Database(
    WorkoutMergeMixin,
    MetricsMixin,
    NutritionMixin,
    JournalMixin,
    ProfileMixin,
    StrengthMixin,
    TrainingPlanMixin,
):
    """SQLModel-backed facade over the health database.

    The domain methods live in repository mixins (one module each — metrics,
    nutrition, journal, profile, strength, training, workout-merge); this class
    owns only the shared infrastructure they build on: the engine, ``session``,
    schema init/migration, the field-preserving ``_upsert``, the ``_view_rows``
    /``_cutoff`` read primitives, and ``backup_to``. Every mixin method reaches
    those through ``self``, so the composition is transparent to callers —
    ``Database`` is still the single facade it always was.
    """

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

    # ── Summary reads (raw SQL against the view) ───────────────────────────────

    def _view_rows(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        with self.session() as s:
            result = s.exec(text(sql).bindparams(**params))
            return [dict(r._mapping) for r in result]

    @staticmethod
    def _cutoff(days: int) -> str:
        """ISO day string ``days`` calendar days back, today inclusive."""
        return (date.today() - timedelta(days=days - 1)).isoformat()

    _PLAN_STATUSES = {"planned", "done", "skipped", "partially_done"}

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
