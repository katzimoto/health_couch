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
    # Today inclusive: the analyzer windows are calendar-based, so a seed that
    # stopped at yesterday would leave today as a (deliberate) hole.
    for i in range(days - 1, -1, -1):
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


def test_partial_upsert_preserves_existing_fields(db: Database) -> None:
    from garmin_coach.models import Weight

    d = date.today()
    db.upsert_weight(d, weight_kg=80.0, body_fat=20.0, muscle_kg=35.0)
    db.upsert_weight(d, weight_kg=79.0)  # e.g. a manual log_weight call
    latest = db.latest_summary()
    assert latest["weight_kg"] == 79.0
    assert latest["body_fat"] == 20.0  # not blanked by the partial write
    with db.session() as s:
        row = s.get(Weight, d.isoformat())
    assert row.muscle_kg == 35.0


def test_repull_with_missing_field_keeps_stored_value(db: Database) -> None:
    d = date.today()
    db.upsert_sleep(d, score=90, total_seconds=8 * 3600)
    # Garmin re-pull where the score isn't computed (yet / anymore).
    db.upsert_sleep(d, score=None, total_seconds=8 * 3600 + 60)
    summary = db.daily_summary(days=1)
    assert summary[0]["sleep_score"] == 90
    assert summary[0]["sleep_hours"] == pytest.approx(8.02, abs=0.01)


def test_analyzer_windows_are_calendar_days(db: Database) -> None:
    # Data only 10-20 days ago; the last week is a genuine gap (watch off).
    for i in range(10, 21):
        d = date.today() - timedelta(days=i)
        db.upsert_sleep(d, score=80, total_seconds=7 * 3600)
    report = Analyzer(db).report()
    trend = report["trends"]["sleep_hours"]
    # Row-slicing would have treated the 7 newest *rows* as "this week".
    assert trend["avg_7d"] is None
    assert trend["avg_28d"] == 7.0
    assert report["sleep_debt_7d"] is None


def test_acr_counts_missing_days_as_rest(db: Database) -> None:
    # Anchor the history span, then train hard only in the last 7 days.
    db.upsert_steps(date.today() - timedelta(days=27), steps=4000)
    for i in range(7):
        d = date.today() - timedelta(days=i)
        db.upsert_workout(2000 + i, d, name="Run", type="running", training_load=70)
    acr = Analyzer(db).acute_chronic_ratio()
    assert acr["acute_7d"] == 70.0
    assert acr["chronic_28d"] == pytest.approx(17.5)  # 7*70 over 28 calendar days
    assert acr["ratio"] == 4.0


def test_acr_ratio_withheld_for_short_history(db: Database) -> None:
    # Only 5 days of history: a 28-day baseline would be fiction, and the
    # ratio would flag a "spike" on a brand-new database.
    for i in range(5):
        d = date.today() - timedelta(days=i)
        db.upsert_workout(3000 + i, d, name="Run", type="running", training_load=50)
    assert Analyzer(db).acute_chronic_ratio()["ratio"] is None


def test_pull_log_roundtrip(db: Database) -> None:
    db.record_pull("2026-07-01", {"sleep": "ok"})
    db.record_pull(date(2026, 7, 2), {"sleep": "ok", "hrv": "error: boom"})
    assert db.pulled_days("2026-06-30", "2026-07-03") == {"2026-07-01", "2026-07-02"}


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
