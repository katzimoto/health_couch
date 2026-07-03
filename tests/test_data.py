"""Offline tests for the storage and analysis layers (no network needed).

Run with ``python -m pytest``. These seed a temp SQLite DB, exercise the upserts
and the daily_summary view, and assert the analyzer's trends/flags behave.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_coach.analysis import Analyzer
from garmin_coach.database import Database


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "test.db"))


def _seed(db: Database, days: int = 30) -> None:
    for i in range(days, 0, -1):
        d = date.today() - timedelta(days=i)
        db.upsert_sleep(d, score=80, total_seconds=7 * 3600, resting_hr=50)
        db.upsert_hrv(d, last_night_avg=60, weekly_avg=60, status="BALANCED")
        db.upsert_resting_hr(d, resting_hr=50)
        db.upsert_steps(d, steps=8000, goal=10000)
        db.upsert_weight(d, weight_kg=80.0, body_fat=20.0)
        db.upsert_workout(1000 + i, d, name="Run", type="running", training_load=50)


def test_upsert_is_idempotent(db: Database) -> None:
    d = date.today()
    db.upsert_sleep(d, score=70, total_seconds=6 * 3600)
    db.upsert_sleep(d, score=95, total_seconds=8 * 3600)  # overwrite same day
    summary = db.daily_summary(days=1)
    assert len(summary) == 1
    assert summary[0]["sleep_score"] == 95
    assert summary[0]["sleep_hours"] == 8.0


def test_daily_summary_joins_families(db: Database) -> None:
    _seed(db, days=5)
    latest = db.latest_summary()
    assert latest is not None
    assert latest["hrv"] == 60
    assert latest["resting_hr"] == 50
    assert latest["steps"] == 8000
    assert latest["workout_count"] == 1
    assert latest["training_load"] == 50


def test_metric_series_rejects_unknown_column(db: Database) -> None:
    with pytest.raises(ValueError):
        db.metric_series("droptable")


def test_meals_logged_and_aggregated_into_summary(db: Database) -> None:
    d = date.today()
    db.add_meal("oatmeal", day=d, calories=400)
    db.add_meal("chicken salad", day=d, calories=650, note="lunch")
    meals = db.recent_meals(days=1)
    assert [m["name"] for m in meals] == ["oatmeal", "chicken salad"]

    summary = db.daily_summary(days=1)
    assert summary[0]["calories_in"] == 1050


def test_manual_weight_log_overwrites_garmin_weight(db: Database) -> None:
    d = date.today()
    db.upsert_weight(d, weight_kg=80.0, body_fat=20.0)
    db.upsert_weight(d, weight_kg=79.5, body_fat=19.5)  # e.g. a manual log_weight call
    latest = db.latest_summary()
    assert latest["weight_kg"] == 79.5
    assert latest["body_fat"] == 19.5


def test_conversation_and_feedback_memory(db: Database) -> None:
    db.add_message("user", "hi")
    db.add_message("assistant", "hello")
    msgs = db.recent_messages(limit=10)
    assert [m["role"] for m in msgs] == ["user", "assistant"]

    db.add_feedback("felt great")
    assert db.recent_feedback()[-1]["note"] == "felt great"

    db.save_plan(date.today(), "plan A")
    assert db.last_plan()["plan"] == "plan A"


def test_analyzer_report_and_flags(db: Database) -> None:
    _seed(db, days=30)
    report = Analyzer(db).report()
    assert report["available"] is True
    assert report["trends"]["hrv"]["avg_7d"] == 60
    # Steady 7h sleep vs 8h target => ~7h debt over the week => flagged.
    assert report["sleep_debt_7d"] == pytest.approx(7.0, abs=0.1)
    assert any("Sleep debt" in f for f in report["flags"])


def test_analyzer_flags_hrv_decline(db: Database) -> None:
    # Strictly declining HRV for the last several days.
    for i in range(10, 0, -1):
        d = date.today() - timedelta(days=i)
        db.upsert_hrv(d, last_night_avg=50 + i)  # decreases as i decreases
    flags = Analyzer(db).flags(db.daily_summary(days=10))
    assert any("HRV down" in f for f in flags)


def test_empty_db_report(db: Database) -> None:
    report = Analyzer(db).report()
    assert report["available"] is False
