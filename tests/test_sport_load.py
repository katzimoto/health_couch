"""Tests for the sport-specific training-load breakdown (issue #10).

Offline over synthetic fixtures: a mixed swim/strength/run history in a temp
SQLite database, plus direct exercise of the pure bucketing and EWMA helpers.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_coach.analysis import Analyzer
from garmin_coach.database import Database
from garmin_coach.sport_load import (
    RATIO_NOTE,
    SPORT_MIN_HISTORY_DAYS,
    acute_chronic,
    bucket_by_sport,
    build_sport_training_load,
    daily_load_series,
    load_by_source,
)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "load.db"))


def _day(offset: int) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


def _workout(db: Database, activity_id: int, offset: int, wtype: str, **fields) -> None:
    db.upsert_workout(activity_id, _day(offset), type=wtype, **fields)


def _mixed_week(db: Database) -> None:
    """One synthetic week: two swims, two runs, three gym sessions."""
    _workout(db, 1, 6, "lap_swimming", name="Swim", duration_s=1800, distance_m=1500,
             training_load=40, source="garmin", load_source="garmin")
    _workout(db, 2, 3, "lap_swimming", name="Swim", duration_s=2100, distance_m=1800,
             training_load=45, source="garmin", load_source="garmin")
    _workout(db, 3, 5, "running", name="Run", duration_s=1800, distance_m=5000,
             training_load=60, source="garmin", load_source="garmin")
    _workout(db, 4, 2, "running", name="Run", duration_s=2400, distance_m=7000,
             training_load=80, source="garmin", load_source="garmin")
    for i, offset in enumerate((6, 4, 1)):
        _workout(db, 10 + i, offset, "strength_training", name="Gym", duration_s=3600,
                 training_load=55, source="manual", load_source="estimated")


# ── Pure helpers ───────────────────────────────────────────────────────────────

def test_buckets_use_the_shared_normalizer() -> None:
    buckets = bucket_by_sport([
        {"activity_id": 1, "day": _day(1), "type": "Running"},
        {"activity_id": 2, "day": _day(1), "type": "run"},
        {"activity_id": 3, "day": _day(1), "type": "treadmill_running"},
        {"activity_id": 4, "day": _day(1), "type": None},
    ])
    assert len(buckets["running"]) == 2          # spellings fold
    assert len(buckets["treadmill_running"]) == 1  # modalities don't
    assert len(buckets["unknown"]) == 1


def test_load_by_source_separates_methods_and_counts_missing() -> None:
    split = load_by_source([
        {"training_load": 60, "load_source": "garmin"},
        {"training_load": 40, "load_source": "estimated"},
        {"training_load": 20, "load_source": "manual"},
        {"training_load": None, "load_source": "garmin"},
    ])
    assert split["totals"] == {"garmin": 60.0, "estimated": 40.0, "manual": 20.0}
    assert split["sessions_without_load"] == 1
    assert "not the same measurement" in split["note"]


def test_daily_series_is_zero_filled_by_calendar_day() -> None:
    start = date.today() - timedelta(days=4)
    series = daily_load_series(
        [
            {"day": (start + timedelta(days=1)).isoformat(), "training_load": 30},
            {"day": (start + timedelta(days=1)).isoformat(), "training_load": 20},
            {"day": (start + timedelta(days=3)).isoformat(), "training_load": 10},
        ],
        start, date.today(),
    )
    assert series == [0.0, 50.0, 0.0, 10.0, 0.0]


def test_acute_chronic_withheld_on_short_history() -> None:
    start = date.today() - timedelta(days=6)
    result = acute_chronic(
        [{"day": _day(1), "training_load": 60}], start, date.today(), label="running"
    )
    assert result["ratio"] is None
    assert f"least {SPORT_MIN_HISTORY_DAYS}" in result["unavailable_reason"]
    assert result["note"] == RATIO_NOTE


def test_acute_chronic_withheld_without_any_load_values() -> None:
    start = date.today() - timedelta(days=30)
    result = acute_chronic(
        [{"day": _day(3), "training_load": None}], start, date.today(), label="yoga"
    )
    assert result["ratio"] is None
    assert "carries a load value" in result["unavailable_reason"]


# ── The report ─────────────────────────────────────────────────────────────────

def test_mixed_week_counts_each_sport_correctly(db: Database) -> None:
    _mixed_week(db)
    report = build_sport_training_load(db, days=7)
    sports = report["by_sport"]
    assert set(sports) == {"lap_swimming", "running", "strength_training"}
    assert sports["lap_swimming"]["session_count"] == 2
    assert sports["running"]["session_count"] == 2
    assert sports["strength_training"]["session_count"] == 3
    assert sports["lap_swimming"]["total_distance_m"] == 3300.0
    assert sports["strength_training"]["total_distance_m"] is None
    assert "do not record a comparable distance" in (
        sports["strength_training"]["distance_unavailable_reason"]
    )


def test_sport_buckets_reconcile_to_the_reported_total(db: Database) -> None:
    _mixed_week(db)
    report = build_sport_training_load(db, days=7)
    assert report["reconciliation"]["reconciles"] is True
    assert report["reconciliation"]["sum_of_sport_load"] == pytest.approx(
        report["reconciliation"]["reported_total_load"]
    )
    # 40 + 45 + 60 + 80 + 55×3 = 390
    assert report["reconciliation"]["reported_total_load"] == pytest.approx(390.0)
    assert report["reconciliation"]["not_summed"] == ["strength_workload"]


def test_merged_canonical_is_not_double_counted(db: Database) -> None:
    _workout(db, 20, 2, "strength_training", name="Gym (Garmin)", duration_s=3500,
             avg_hr=120, training_load=80, source="garmin", load_source="garmin",
             start_time=f"{_day(2)} 18:00:00")
    _workout(db, -20, 2, "strength_training", name="Push day", duration_s=3400,
             training_load=70, source="manual", load_source="estimated",
             start_time=f"{_day(2)} 18:00:00")
    merged = db.merge_workout_sources(day=_day(2))
    assert merged["merged"] is True

    report = build_sport_training_load(db, days=7)
    strength = report["by_sport"]["strength_training"]
    assert strength["session_count"] == 1
    assert strength["total_training_load"] == 80.0
    assert report["reconciliation"]["reconciles"] is True


def test_overall_metric_is_unchanged(db: Database) -> None:
    _mixed_week(db)
    report = build_sport_training_load(db, days=7)
    assert report["overall"]["ratio"] == Analyzer(db).acute_chronic_ratio()["ratio"]
    assert report["overall"]["acute_7d"] == Analyzer(db).acute_chronic_ratio()["acute_7d"]
    assert "unchanged legacy metric" in report["overall"]["method"]


def test_single_sport_history_matches_the_overall_ratio(db: Database) -> None:
    """Partitioning must be a partition: one sport → the same numbers."""
    for i in range(30):
        _workout(db, 100 + i, i, "running", name="Run", duration_s=1800,
                 distance_m=5000, training_load=50, source="garmin",
                 load_source="garmin")
    report = build_sport_training_load(db, days=28)
    overall = Analyzer(db).acute_chronic_ratio()
    running = report["by_sport"]["running"]["acute_chronic"]
    assert running["acute_7d"] == pytest.approx(overall["acute_7d"], abs=0.05)
    assert running["ratio"] == pytest.approx(overall["ratio"], abs=0.02)


def test_strength_workload_visible_without_device_hr_or_load(db: Database) -> None:
    session = db.add_strength_session(
        _day(2), duration_s=3600,
        exercises=[
            {"exercise_name": "bench press",
             "actual_sets": [{"reps": 10, "weight_kg": 60}] * 3},
            {"exercise_name": "squat",
             "actual_sets": [{"reps": 5, "weight_kg": 100}] * 3},
        ],
    )
    # Strip the device-style fields entirely.
    db.update_workout(session["activity_id"], avg_hr=None, training_load=None)
    report = build_sport_training_load(db, days=7)
    strength = report["by_sport"]["strength_training"]["strength_workload"]
    assert strength["completed_working_sets"] == 6
    assert strength["completed_volume_kg"] == pytest.approx(1800 + 1500)
    assert strength["volume_basis"] == "per_set"
    assert strength["units"]["completed_volume_kg"] == "kilograms × reps"
    assert "never added to cardiovascular load" in strength["note"]


def test_estimated_and_device_loads_are_reported_separately(db: Database) -> None:
    _mixed_week(db)
    report = build_sport_training_load(db, days=7)
    gym = report["by_sport"]["strength_training"]["load_by_source"]
    assert gym["totals"] == {"estimated": 165.0}
    swim = report["by_sport"]["lap_swimming"]["load_by_source"]
    assert swim["totals"] == {"garmin": 85.0}


def test_session_without_load_is_missing_not_zero(db: Database) -> None:
    _workout(db, 30, 1, "yoga", name="Yoga", duration_s=1800, source="manual")
    report = build_sport_training_load(db, days=7)
    yoga = report["by_sport"]["yoga"]
    assert yoga["coverage"]["sessions_with_load"] == 0
    assert yoga["coverage"]["sessions_total"] == 1
    assert "missing data, not zero load" in yoga["coverage"]["note"]
    assert yoga["acute_chronic"]["ratio"] is None


def test_rest_is_distinguished_from_a_missing_sync(db: Database) -> None:
    _workout(db, 40, 3, "running", name="Run", duration_s=1800, distance_m=5000,
             training_load=50, source="garmin", load_source="garmin")
    # Two days recorded a successful pull; the rest of the window never synced.
    db.record_pull(_day(3), {"workouts": "ok"})
    db.record_pull(_day(2), {"workouts": "ok"})
    report = build_sport_training_load(db, days=5)
    rest = report["rest_and_coverage"]
    assert rest["active_days"] == 1
    assert rest["confirmed_rest_days"] == 1   # the synced day with no session
    assert rest["unknown_days"] == 3
    assert "no recorded sync" in rest["distinction"]


def test_flagged_recordings_are_excluded_and_listed(db: Database) -> None:
    _workout(db, 50, 2, "running", name="Good", duration_s=1800, distance_m=5000,
             training_load=60, source="garmin", load_source="garmin")
    _workout(db, 51, 1, "running", name="Glitch", duration_s=600, distance_m=9000,
             training_load=999, source="garmin", load_source="garmin")
    report = build_sport_training_load(db, days=7)
    assert 51 in report["data_quality"]["excluded_activity_ids"]
    assert report["by_sport"]["running"]["total_training_load"] == 60.0
    assert 51 in report["by_sport"]["running"]["excluded_activity_ids"]


def test_filtering_to_one_sport(db: Database) -> None:
    _mixed_week(db)
    report = build_sport_training_load(db, days=7, sport="swim")
    assert set(report["by_sport"]) == {"lap_swimming"}
    assert report["filtered_to_sport"] == "lap_swimming"
    assert report["reconciliation"]["reconciles"] is None
    assert "one sport" in report["reconciliation"]["note"]


def test_filtering_to_an_unrecorded_sport_is_explicit(db: Database) -> None:
    _mixed_week(db)
    report = build_sport_training_load(db, days=7, sport="cycling")
    assert report["by_sport"] == {}
    assert "no cycling sessions recorded" in report["unavailable_reason"]


def test_empty_database_is_safe(db: Database) -> None:
    report = build_sport_training_load(db, days=28)
    assert report["by_sport"] == {}
    assert report["reconciliation"]["reported_total_load"] == 0.0
    assert report["rest_and_coverage"]["active_days"] == 0


def test_existing_get_training_load_callers_still_work(db: Database) -> None:
    """The legacy tool contract is unchanged; the breakdown is opt-in."""
    import garmin_coach.mcp_server as mcp_server

    _mixed_week(db)
    legacy = mcp_server.get_training_load(days=28)
    assert set(legacy) == {"acute_chronic", "recent_workouts", "merged_workouts"}

    with_breakdown = mcp_server.get_training_load(days=28, by_sport=True)
    assert set(with_breakdown) == {
        "acute_chronic", "recent_workouts", "merged_workouts", "sport_breakdown"
    }
    assert with_breakdown["acute_chronic"] == legacy["acute_chronic"]
