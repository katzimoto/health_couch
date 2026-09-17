"""Tests for the generic activity-progression service (issue #7).

All offline and deterministic: synthetic workouts in a temp SQLite database
plus direct exercise of the pure aggregation functions. Nothing here talks to
Garmin, OpenAI or the network, and no personal data is used.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_coach.activity_progress import (
    MIN_PACE_DISTANCE_M,
    activity_family,
    build_activity_progress,
    compare,
    linear_slope,
    matches_type,
    metric_profile,
    normalize_activity_type,
    observed_records,
    pace_seconds,
    percent_change,
    recorded_activity_types,
    related_types,
    summarize_sessions,
    week_buckets,
    weekly_series,
    weekly_trends,
    window_bounds,
)
from garmin_coach.database import Database


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "progress.db"))


def _day(offset: int) -> str:
    """ISO day ``offset`` days ago."""
    return (date.today() - timedelta(days=offset)).isoformat()


def _session(activity_id: int, offset: int, **fields) -> dict:
    return {"activity_id": activity_id, "day": _day(offset), **fields}


# ── Type normalization: synonyms fold, modalities don't ─────────────────────────

def test_aliases_fold_only_spellings_of_the_same_modality() -> None:
    assert normalize_activity_type("Running") == "running"
    assert normalize_activity_type("  road running ") == "running"
    assert normalize_activity_type("swim") == "lap_swimming"
    assert normalize_activity_type("traditional_strength_training") == "strength_training"
    # An unknown type is normalized, not dropped.
    assert normalize_activity_type("obstacle-run") == "obstacle_run"
    assert normalize_activity_type(None) is None
    assert normalize_activity_type("   ") is None


def test_materially_different_modalities_stay_distinct() -> None:
    assert normalize_activity_type("open_water_swimming") != normalize_activity_type("lap_swimming")
    assert normalize_activity_type("indoor_cycling") != normalize_activity_type("cycling")
    assert normalize_activity_type("treadmill_running") != normalize_activity_type("running")
    assert "open_water_swimming" in related_types("lap_swimming")
    assert not matches_type("open_water_swimming", "lap_swimming")
    assert matches_type("Running", "run")


def test_metric_profile_per_family() -> None:
    assert metric_profile("running")["pace"] == "min_per_km"
    assert metric_profile("lap_swimming")["pace"] == "min_per_100m"
    assert metric_profile("rowing")["pace"] == "min_per_500m"
    # Cycling is reported as speed, not pace.
    assert metric_profile("cycling")["pace"] is None
    assert metric_profile("cycling")["speed"] == "kph"
    # Strength has neither distance nor pace, and delegates.
    strength = metric_profile("strength_training")
    assert strength["distance"] is False and strength["pace"] is None
    assert strength["delegates_to"] == "get_strength_progress"
    assert metric_profile("lap_swimming")["delegates_to"] == "get_swimming_progress"
    assert activity_family("unknown_thing") == "other"
    assert activity_family("obstacle_run") == "run"


# ── Window and week maths ──────────────────────────────────────────────────────

def test_window_bounds_is_inclusive_of_today() -> None:
    start, end = window_bounds(7, date(2026, 3, 10))
    assert (start, end) == ("2026-03-04", "2026-03-10")


def test_week_buckets_mark_partial_weeks() -> None:
    # 2026-03-04 is a Wednesday, so the first bucket is clipped.
    buckets = week_buckets("2026-03-04", "2026-03-17")
    assert [b["partial"] for b in buckets] == [True, False, True]
    assert buckets[0]["days_covered"] == 5
    assert buckets[1]["week_start"] == "2026-03-09"
    assert buckets[1]["days_covered"] == 7


def test_percent_change_refuses_zero_and_missing_baselines() -> None:
    assert percent_change(100, 110) == 10.0
    assert percent_change(0, 110) is None
    assert percent_change(None, 110) is None
    assert percent_change(100, None) is None


def test_linear_slope_needs_two_distinct_points() -> None:
    assert linear_slope([(0.0, 1.0)]) is None
    assert linear_slope([(0.0, 1.0), (1.0, 3.0), (2.0, 5.0)]) == pytest.approx(2.0)


def test_pace_seconds_is_total_over_total() -> None:
    # 5 km in 25 minutes → 300 s/km.
    assert pace_seconds(5000, 1500, "min_per_km") == 300.0
    assert pace_seconds(0, 1500, "min_per_km") is None
    assert pace_seconds(5000, 0, "min_per_km") is None


# ── Aggregation semantics ──────────────────────────────────────────────────────

def test_aggregate_pace_is_weighted_not_an_average_of_paces() -> None:
    """A long slow session must dominate a tiny fast one."""
    sessions = [
        _session(1, 3, distance_m=1000, duration_s=240),    # 4:00/km over 1 km
        _session(2, 2, distance_m=10000, duration_s=3600),  # 6:00/km over 10 km
    ]
    summary = summarize_sessions(sessions, "running", window_days=7)
    # Weighted: 3840 s over 11 km = 349.1 s/km. The naive mean of paces (300)
    # would be wrong by nearly a minute per kilometre.
    assert summary["pace"]["seconds_per_unit"] == pytest.approx(349.1, abs=0.2)
    assert summary["pace"]["sample_count"] == 2
    assert "total distance" in summary["pace"]["method"]


def test_short_sessions_count_for_volume_but_not_pace() -> None:
    sessions = [
        _session(1, 2, distance_m=MIN_PACE_DISTANCE_M - 1, duration_s=60),
        _session(2, 1, distance_m=5000, duration_s=1500),
    ]
    summary = summarize_sessions(sessions, "running", window_days=7)
    assert summary["workout_count"] == 2
    assert summary["total_distance_m"] == pytest.approx(5199.0)
    assert summary["pace"]["sample_count"] == 1
    assert summary["pace"]["seconds_per_unit"] == 300.0


def test_active_duration_preferred_over_elapsed_when_present() -> None:
    sessions = [_session(1, 1, distance_m=2000, duration_s=2400, active_duration_s=1800)]
    summary = summarize_sessions(sessions, "lap_swimming", window_days=7)
    assert summary["time_basis"] == "active_duration"
    # 1800 s over 2000 m = 90 s / 100 m, using the active clock not the elapsed.
    assert summary["pace"]["seconds_per_unit"] == 90.0


def test_strength_reports_no_distance_or_pace_with_reasons() -> None:
    sessions = [_session(1, 1, duration_s=3600, training_load=60)]
    summary = summarize_sessions(sessions, "strength_training", window_days=7)
    assert summary["total_distance_m"] is None
    assert summary["distance_available"] is False
    assert "do not record a meaningful distance" in summary["distance_unavailable_reason"]
    assert summary["pace"] is None
    assert summary["pace_unavailable_reason"]
    assert summary["total_duration_s"] == 3600.0


def test_avg_hr_is_duration_weighted_with_coverage() -> None:
    sessions = [
        _session(1, 2, duration_s=600, avg_hr=120),
        _session(2, 1, duration_s=3000, avg_hr=150),
        _session(3, 1, duration_s=1800),  # no HR at all
    ]
    summary = summarize_sessions(sessions, "running", window_days=7)
    assert summary["avg_hr"] == pytest.approx(145.0)
    assert summary["hr_coverage"] == {"sessions_with_hr": 2, "sessions_total": 3}


def test_excluded_sessions_still_count_as_sessions() -> None:
    sessions = [
        _session(1, 2, distance_m=0, duration_s=1800),
        _session(2, 1, distance_m=5000, duration_s=1500),
    ]
    summary = summarize_sessions(
        sessions, "running", window_days=7, excluded_activity_ids=[1]
    )
    assert summary["workout_count"] == 1
    assert summary["excluded_activity_ids"] == [1]
    assert summary["pace"]["seconds_per_unit"] == 300.0


def test_empty_window_is_zero_volume_but_null_metrics() -> None:
    summary = summarize_sessions([], "running", window_days=30)
    assert summary["workout_count"] == 0
    assert summary["avg_session_duration_s"] is None
    assert summary["total_distance_m"] is None
    assert summary["pace"] is None


# ── Comparison and trends ──────────────────────────────────────────────────────

def test_lower_pace_time_reads_as_improvement() -> None:
    current = summarize_sessions(
        [_session(1, 1, distance_m=5000, duration_s=1450)], "running", window_days=30
    )
    baseline = summarize_sessions(
        [_session(9, 40, distance_m=5000, duration_s=1500)], "running", window_days=30
    )
    result = compare(current, baseline, "running")
    assert result["pace"]["absolute_change_s"] == -10.0
    assert result["pace"]["improved"] is True
    assert result["pace"]["interpretation"] == "faster than baseline"
    assert "higher heart rate" in result["pace"]["caveat"]


def test_missing_baseline_yields_unavailable_not_infinite_growth() -> None:
    current = summarize_sessions(
        [_session(1, 1, distance_m=5000, duration_s=1500)], "running", window_days=30
    )
    baseline = summarize_sessions([], "running", window_days=30)
    result = compare(current, baseline, "running")
    assert result["weekly_distance_m"]["baseline"] is None
    assert result["weekly_distance_m"]["percent_change"] is None
    assert result["weekly_distance_m"]["unavailable_reason"]
    assert result["pace"] is None


def test_weekly_trends_exclude_partial_weeks() -> None:
    start, end = "2026-03-02", "2026-03-24"  # Monday → Tuesday
    sessions = [
        {"activity_id": 1, "day": "2026-03-03", "distance_m": 5000, "duration_s": 1500},
        {"activity_id": 2, "day": "2026-03-10", "distance_m": 6000, "duration_s": 1800},
        {"activity_id": 3, "day": "2026-03-17", "distance_m": 7000, "duration_s": 2100},
        {"activity_id": 4, "day": "2026-03-24", "distance_m": 9000, "duration_s": 2700},
    ]
    series = weekly_series(sessions, start, end)
    assert [w["distance_m"] for w in series] == [5000.0, 6000.0, 7000.0, 9000.0]
    trends = weekly_trends(series)
    assert trends["excluded_partial_weeks"] == 1  # the clipped final week
    assert trends["sample_weeks"] == 3
    assert trends["distance_m"]["slope_per_week"] == pytest.approx(1000.0)
    assert trends["distance_m"]["direction"] == "increasing"
    assert trends["distance_m"]["unit"] == "metres per week"


def test_weekly_trends_unavailable_below_two_complete_weeks() -> None:
    series = weekly_series([], "2026-03-04", "2026-03-08")
    trends = weekly_trends(series)
    assert trends["available"] is False
    assert "two complete calendar weeks" in trends["unavailable_reason"]


def test_observed_records_are_labelled_as_session_level() -> None:
    sessions = [
        _session(1, 3, distance_m=5000, duration_s=1500, avg_hr=150),
        _session(2, 2, distance_m=10000, duration_s=3300, avg_hr=155),
    ]
    records = observed_records(sessions, "running")
    assert records["longest_distance_m"]["value"] == 10000
    assert records["longest_distance_m"]["activity_id"] == 2
    assert records["best_session_pace"]["activity_id"] == 1
    assert records["best_session_pace"]["seconds_per_unit"] == 300.0
    assert "not continuous-effort personal records" in records["caveat"]
    assert observed_records([], "running")["available"] is False


# ── End-to-end over the database ───────────────────────────────────────────────

def _seed_runs(db: Database) -> None:
    """Baseline window: 3 slow runs. Analysis window: 4 faster runs."""
    for i, offset in enumerate((45, 52, 59)):
        db.upsert_workout(
            100 + i, _day(offset), name="Baseline run", type="running",
            duration_s=1800, distance_m=5000, avg_hr=150,
            training_load=60, source="garmin", load_source="garmin",
        )
    for i, offset in enumerate((3, 10, 17, 24)):
        db.upsert_workout(
            200 + i, _day(offset), name="Run", type="running",
            duration_s=1500, distance_m=5000, avg_hr=152,
            training_load=70, source="garmin", load_source="garmin",
        )


def test_end_to_end_running_progress(db: Database) -> None:
    _seed_runs(db)
    report = build_activity_progress(db, "running", days=30)

    assert report["available"] is True
    assert report["activity_type"] == "running"
    assert report["analysis_window"]["days"] == 30
    assert report["baseline_window"]["days"] == 30
    assert report["baseline_window"]["end"] < report["analysis_window"]["start"]
    assert report["summary"]["workout_count"] == 4
    assert report["baseline_summary"]["workout_count"] == 3
    # 5 km in 25 min now vs 30 min before.
    assert report["summary"]["pace"]["seconds_per_unit"] == 300.0
    assert report["baseline_summary"]["pace"]["seconds_per_unit"] == 360.0
    assert report["comparison"]["pace"]["improved"] is True
    assert report["comparison"]["weekly_distance_m"]["percent_change"] is not None
    assert report["records"]["best_session_pace"]["seconds_per_unit"] == 300.0
    assert report["coverage"]["window_days"] == 30
    assert "not confirmed rest days" in report["coverage"]["note"]


def test_other_sports_are_not_pooled_into_running(db: Database) -> None:
    _seed_runs(db)
    db.upsert_workout(
        300, _day(2), name="Pool swim", type="lap_swimming",
        duration_s=1800, distance_m=1500, source="garmin",
    )
    db.upsert_workout(
        301, _day(2), name="Treadmill", type="treadmill_running",
        duration_s=1800, distance_m=5000, source="garmin",
    )
    running = build_activity_progress(db, "running", days=30)
    assert running["summary"]["workout_count"] == 4  # treadmill not absorbed
    assert "treadmill_running" in running["related_types_not_included"]

    swimming = build_activity_progress(db, "lap_swimming", days=30)
    assert swimming["summary"]["workout_count"] == 1
    assert swimming["summary"]["pace"]["unit"] == "min_per_100m"
    assert swimming["delegation"]["tool"] == "get_swimming_progress"


def test_unsupported_sport_metrics_have_reasons_not_values(db: Database) -> None:
    db.upsert_workout(
        400, _day(1), name="Gym", type="strength_training",
        duration_s=3600, training_load=55, source="manual", load_source="estimated",
    )
    report = build_activity_progress(db, "strength_training", days=30)
    assert report["summary"]["total_distance_m"] is None
    assert report["summary"]["pace"] is None
    assert report["summary"]["pace_unavailable_reason"]
    assert report["delegation"]["tool"] == "get_strength_progress"
    assert report["summary"]["total_training_load"] == 55.0


def test_unknown_stored_type_still_returns_generic_metrics(db: Database) -> None:
    db.upsert_workout(
        500, _day(1), name="Row", type="rowing",
        duration_s=1800, distance_m=6000, source="garmin",
    )
    report = build_activity_progress(db, "rowing", days=30)
    assert report["available"] is True
    assert report["summary"]["pace"]["unit"] == "min_per_500m"
    assert report["summary"]["pace"]["seconds_per_unit"] == 150.0


def test_no_sessions_returns_partial_report_not_invented_numbers(db: Database) -> None:
    report = build_activity_progress(db, "cycling", days=30)
    assert report["available"] is False
    assert "no cycling sessions recorded" in report["unavailable_reason"]
    assert report["summary"]["workout_count"] == 0
    assert report["summary"]["pace"] is None
    assert report["comparison"]["weekly_distance_m"]["percent_change"] is None


def test_merged_and_duplicate_rows_are_counted_once(db: Database) -> None:
    """A session recorded twice must not inflate count, volume or load."""
    db.upsert_workout(
        600, _day(2), name="Run (Garmin)", type="running", duration_s=1500,
        distance_m=5000, training_load=70, source="garmin", load_source="garmin",
    )
    db.upsert_workout(
        601, _day(2), name="Run (Apple)", type="running", duration_s=1500,
        distance_m=5000, training_load=68, source="apple", load_source="estimated",
    )
    db.dedupe_workouts(days=30)
    report = build_activity_progress(db, "running", days=30)
    assert report["summary"]["workout_count"] == 1
    assert report["summary"]["total_distance_m"] == 5000.0
    assert report["summary"]["total_training_load"] == 70.0


def test_zero_distance_run_is_flagged_and_excluded_from_pace(db: Database) -> None:
    db.upsert_workout(
        700, _day(2), name="Broken GPS run", type="running",
        duration_s=1800, distance_m=0, source="garmin",
    )
    db.upsert_workout(
        701, _day(1), name="Good run", type="running",
        duration_s=1500, distance_m=5000, source="garmin",
    )
    report = build_activity_progress(db, "running", days=30)
    assert 700 in report["data_quality"]["excluded_from_distance_and_pace"]
    assert report["summary"]["workout_count"] == 1  # the flagged one is excluded
    assert report["summary"]["pace"]["seconds_per_unit"] == 300.0
    assert any(w["activity_id"] == 700 for w in report["data_quality"]["warnings"])


def test_window_lengths_are_respected(db: Database) -> None:
    _seed_runs(db)
    for days, expected in ((7, 1), (30, 4), (60, 7)):
        report = build_activity_progress(db, "running", days=days)
        assert report["summary"]["workout_count"] == expected, days
        assert report["analysis_window"]["days"] == days


def test_include_sessions_is_optional_and_bounded(db: Database) -> None:
    _seed_runs(db)
    without = build_activity_progress(db, "running", days=30)
    assert "sessions" not in without
    with_sessions = build_activity_progress(
        db, "running", days=30, include_sessions=True, session_limit=2
    )
    assert len(with_sessions["sessions"]) == 2
    assert with_sessions["sessions_truncated"] is True
    assert with_sessions["sessions"][0]["activity_id"] is not None


def test_recorded_activity_types_lists_every_sport(db: Database) -> None:
    _seed_runs(db)
    db.upsert_workout(800, _day(1), name="Swim", type="lap_swimming", duration_s=1800)
    db.upsert_workout(801, _day(1), name="Gym", type="strength_training", duration_s=3600)
    types = {t["activity_type"]: t for t in recorded_activity_types(db, days=30)}
    assert set(types) == {"running", "lap_swimming", "strength_training"}
    assert types["running"]["workout_count"] == 4
    assert types["lap_swimming"]["family"] == "swim"
