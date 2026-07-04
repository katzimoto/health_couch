"""Migration operations invoked by the Alembic revision scripts.

Plain-``Connection`` functions so each operation is unit-testable without an
Alembic context. Every operation is guarded/idempotent: a live database may
already contain the change (the startup column reconciler added the meal
macro columns before Alembic arrived), and sibling containers can race the
upgrade at boot — re-applying must be harmless.

The Alembic environment itself lives in ``garmin_coach/alembic`` (inside the
package so the Docker image ships it); ``Database.init_schema`` upgrades to
head on every startup under a cross-container file lock.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from sqlalchemy.engine import Connection

_MEAL_MACRO_COLUMNS = ("protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g")


def _existing_columns(conn: Connection, table: str) -> set[str]:
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info('{table}')")}


def _table_exists(conn: Connection, table: str) -> bool:
    return bool(_existing_columns(conn, table))


def add_meal_macro_columns(conn: Connection) -> None:
    """Databases created before the Meal model gained macro fields fail every
    log_meal INSERT that names them (the ChatGPT connector regression).

    No-op when the table doesn't exist yet (bare ``alembic upgrade head`` on
    an empty database) — ``create_all`` builds it in final shape.
    """
    if not _table_exists(conn, "meal"):
        return
    existing = _existing_columns(conn, "meal")
    for column in _MEAL_MACRO_COLUMNS:
        if column not in existing:
            conn.exec_driver_sql(f'ALTER TABLE meal ADD COLUMN "{column}" FLOAT')


def drop_meal_macro_columns(conn: Connection) -> None:
    if not _table_exists(conn, "meal"):
        return
    existing = _existing_columns(conn, "meal")
    for column in _MEAL_MACRO_COLUMNS:
        if column in existing:
            conn.exec_driver_sql(f'ALTER TABLE meal DROP COLUMN "{column}"')


_ASSUMED_STATUS = json.dumps({"assumed": "data predates pull_log"})


def mark_existing_days_pulled(conn: Connection) -> None:
    """Seed pull_log for days that already hold Garmin-fed data.

    pull_log arrived after most history was pulled, so on upgraded databases
    the gap healer would treat the whole backfill window as missing and spend
    ~2 weeks of scarce Garmin rate-limit budget re-pulling days it already
    has. A day with any wearable data is not a hole. INSERT OR IGNORE keeps
    genuine pull records; today is excluded so the daily pull still refreshes
    it. Trade-off: a marked day won't be re-fetched for metrics added later —
    scripts/backfill.py remains the tool for forced refreshes.
    """
    garmin_fed = (
        "sleep", "resting_hr", "hrv", "stress", "body_battery",
        "steps", "weight", "hydration", "workout",
    )
    # Bare `alembic upgrade head` on an empty DB: nothing to backfill (and
    # nothing to backfill from) until create_all has built the tables.
    present = [t for t in garmin_fed if _table_exists(conn, t)]
    if not present or not _table_exists(conn, "pull_log"):
        return
    union = " UNION ".join(f"SELECT day FROM {table}" for table in present)
    conn.exec_driver_sql(
        "INSERT OR IGNORE INTO pull_log (day, ts, status) "
        f"SELECT DISTINCT day, ?, ? FROM ({union}) WHERE day < ?",
        (
            datetime.now(timezone.utc).isoformat(),
            _ASSUMED_STATUS,
            date.today().isoformat(),
        ),
    )


def unmark_assumed_pulled_days(conn: Connection) -> None:
    """Remove only the rows the forward migration created — genuine pull
    records carry a per-metric status, never the 'assumed' marker."""
    if not _table_exists(conn, "pull_log"):
        return
    conn.exec_driver_sql(
        "DELETE FROM pull_log WHERE status = ?", (_ASSUMED_STATUS,)
    )
