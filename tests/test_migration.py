"""Offline tests for startup schema migration and meal logging robustness.

Reproduces the production failure: a ``meal`` table created before the macro
columns existed makes every INSERT that names them fail with
``sqlite3.OperationalError: table meal has no column named protein_g`` —
``create_all`` never alters existing tables. ``Database.init_schema`` must
reconcile the columns additively, losslessly, and idempotently.
"""

from __future__ import annotations

import sqlite3

import pytest

from garmin_coach.database import Database

_MACRO_COLUMNS = {"protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g"}


def _make_legacy_db(path: str) -> None:
    """A meal table as it existed before macros, with one pre-existing row."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE meal (
               id INTEGER PRIMARY KEY,
               day VARCHAR NOT NULL,
               ts DATETIME NOT NULL,
               name VARCHAR NOT NULL,
               calories INTEGER,
               note VARCHAR
           )"""
    )
    conn.execute(
        "INSERT INTO meal (day, ts, name, calories, note) "
        "VALUES ('2026-06-30', '2026-06-30 12:00:00', 'legacy oatmeal', 350, NULL)"
    )
    conn.commit()
    conn.close()


def _meal_columns(path: str) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info('meal')")}
    finally:
        conn.close()


@pytest.fixture()
def legacy_path(tmp_path) -> str:
    path = str(tmp_path / "legacy.db")
    _make_legacy_db(path)
    return path


def test_migration_adds_macro_columns_to_legacy_table(legacy_path: str) -> None:
    assert not _MACRO_COLUMNS & _meal_columns(legacy_path)  # truly legacy
    Database(path=legacy_path)
    assert _MACRO_COLUMNS <= _meal_columns(legacy_path)


def test_migration_preserves_existing_rows(legacy_path: str) -> None:
    db = Database(path=legacy_path)
    meals = db.recent_meals(days=30)
    legacy = [m for m in meals if m["name"] == "legacy oatmeal"]
    assert len(legacy) == 1
    assert legacy[0]["calories"] == 350
    assert legacy[0]["protein_g"] is None  # backfilled as NULL, not garbage


def test_migration_is_idempotent(legacy_path: str) -> None:
    for _ in range(3):  # every service boot runs init_schema
        Database(path=legacy_path)
    columns = _meal_columns(legacy_path)
    assert _MACRO_COLUMNS <= columns
    assert len(columns) == len(_meal_columns(legacy_path))  # no dupes/renames


def test_legacy_db_accepts_meals_after_migration(legacy_path: str) -> None:
    db = Database(path=legacy_path)
    # The exact insert that failed in production:
    db.add_meal(
        name="Hummus with 2 pita breads",
        day="2026-07-04",
        calories=900,
        note=(
            "Estimated from description only. Likely range: ~600–1,200 kcal "
            "depending on hummus portion size."
        ),
    )
    names = {m["name"] for m in db.recent_meals(days=30)}
    assert {"legacy oatmeal", "Hummus with 2 pita breads"} <= names


def test_mcp_log_meal_hummus_end_to_end(tmp_path, monkeypatch) -> None:
    """The failing production call, through the actual MCP tool functions."""
    import importlib

    monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy_mcp.db"))
    _make_legacy_db(str(tmp_path / "legacy_mcp.db"))
    # Reload config + every module holding settings/db at import time (same
    # pattern as test_web) so the tools bind to the legacy temp DB.
    import garmin_coach.config as config
    importlib.reload(config)
    import garmin_coach.database as database
    importlib.reload(database)
    import garmin_coach.analysis as analysis
    importlib.reload(analysis)
    import garmin_coach.mcp_server as mcp_server
    importlib.reload(mcp_server)

    try:
        result = mcp_server.log_meal(
            name="Hummus with 2 pita breads",
            day="2026-07-04",
            calories=900,
            note=(
                "Estimated from description only. Likely range: ~600–1,200 "
                "kcal depending on hummus portion size."
            ),
        )
        assert result["logged"] is True
        assert result["day"] == "2026-07-04"

        meals = mcp_server.get_meals(days=7)
        hummus = [m for m in meals if m["name"] == "Hummus with 2 pita breads"]
        assert len(hummus) == 1
        assert hummus[0]["calories"] == 900
        assert hummus[0]["protein_g"] is None  # macros stay optional
    finally:
        # Restore module state for tests that import these afterwards.
        monkeypatch.delenv("DB_PATH", raising=False)
        importlib.reload(config)
        importlib.reload(database)
        importlib.reload(analysis)
        importlib.reload(mcp_server)


def test_meal_logging_with_and_without_macros(tmp_path) -> None:
    db = Database(path=str(tmp_path / "fresh.db"))
    db.add_meal(name="calorie only", day="2026-07-04", calories=500)
    db.add_meal(
        name="full macros", day="2026-07-04", calories=900,
        protein_g=28.0, carbs_g=115.0, fat_g=35.0, fiber_g=16.0, sugar_g=6.0,
    )
    db.add_meal(name="partial macros", day="2026-07-04", calories=700, protein_g=40.0)

    meals = {m["name"]: m for m in db.recent_meals(days=7)}
    assert meals["calorie only"]["protein_g"] is None
    assert meals["full macros"]["carbs_g"] == 115.0
    assert meals["partial macros"]["protein_g"] == 40.0
    assert meals["partial macros"]["fat_g"] is None
