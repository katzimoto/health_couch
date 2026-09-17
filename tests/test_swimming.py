"""Tests for swim detail ingestion and swimming analytics (issue #6).

Synthetic fixtures only — a fake Garmin API with canned payload shapes, and a
temp SQLite database. No network, no personal data.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import garmin_coach.garmin_client as garmin_client
from garmin_coach.database import Database
from garmin_coach.garmin_client import GarminClient
from garmin_coach.swimming import (
    aggregate_sessions,
    best_continuous_effort,
    build_session,
    build_swimming_progress,
    comparability_key,
    continuous_bests,
    contiguous_runs,
    efficiency_by_comparability,
    efficiency_metrics,
    normalize_stroke,
    pool_length_meters,
    session_time_breakdown,
    split_active_and_rest,
    window_continuous_bests,
)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "swim.db"))


def _day(offset: int) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


def _lengths(
    count: int,
    seconds: float,
    *,
    distance: float = 25.0,
    stroke: str = "freestyle",
    start_index: int = 1,
    swolf: float | None = 40.0,
    strokes: int | None = 16,
) -> list[dict]:
    return [
        {
            "length_index": start_index + i,
            "duration_s": seconds,
            "distance_m": distance,
            "stroke": stroke,
            "strokes": strokes,
            "swolf": swolf,
            "is_rest": False,
        }
        for i in range(count)
    ]


def _rest(index: int, seconds: float) -> dict:
    return {
        "length_index": index, "duration_s": seconds, "distance_m": 0.0,
        "stroke": None, "strokes": None, "swolf": None, "is_rest": True,
        "split_type": "INTERVAL_REST",
    }


# ── Units, strokes, comparability ──────────────────────────────────────────────

def test_pool_length_conversion_requires_a_known_unit() -> None:
    assert pool_length_meters(25, "meter") == 25.0
    assert pool_length_meters(25, "yard") == pytest.approx(22.86)
    # A bare "25" that might be metres or yards is not a comparable length.
    assert pool_length_meters(25, None) is None
    assert pool_length_meters(25, "furlong") is None
    assert pool_length_meters(0, "meter") is None


def test_stroke_normalization_keeps_unknown_labels() -> None:
    assert normalize_stroke("FREESTYLE") == "freestyle"
    assert normalize_stroke("free") == "freestyle"
    assert normalize_stroke("IM") == "individual_medley"
    assert normalize_stroke("some_new_stroke") == "some_new_stroke"
    assert normalize_stroke(None) is None
    assert normalize_stroke("UNKNOWN") is None


def test_comparability_key_separates_yards_from_metres() -> None:
    metres = {"pool_length_raw": 25, "pool_length_unit": "meter", "primary_stroke": "freestyle"}
    yards = {"pool_length_raw": 25, "pool_length_unit": "yard", "primary_stroke": "freestyle"}
    assert comparability_key(metres) != comparability_key(yards)


# ── Time semantics ─────────────────────────────────────────────────────────────

def test_rest_from_provider_marked_intervals() -> None:
    lengths = _lengths(4, 20.0) + [_rest(5, 30.0)] + _lengths(4, 20.0, start_index=6)
    split = split_active_and_rest(lengths)
    assert split["active_lengths"] == 8
    assert split["rest_intervals"] == 1
    assert split["active_duration_s"] == 160.0
    assert split["rest_duration_s"] == 30.0
    assert split["distance_m"] == 200.0


def test_rest_derived_only_from_compatible_durations() -> None:
    from_marked = session_time_breakdown(
        elapsed_s=1800, timer_s=1700, active_s=1500, lengths_rest_s=280.0
    )
    assert from_marked["rest_duration_s"] == 280.0
    assert from_marked["rest_source"] == "sum of provider-marked rest intervals"

    derived = session_time_breakdown(elapsed_s=1800, timer_s=None, active_s=1500)
    assert derived["rest_duration_s"] == 300.0
    assert derived["rest_source"] == "elapsed_duration − active_duration"

    # Timer time is never silently used as active time.
    elapsed_only = session_time_breakdown(elapsed_s=1800, timer_s=1700, active_s=None)
    assert elapsed_only["active_time_available"] is False
    assert elapsed_only["rest_duration_s"] is None
    assert "no compatible pair" in elapsed_only["rest_unavailable_reason"]
    assert elapsed_only["active_time_unavailable_reason"]


# ── Continuous efforts ─────────────────────────────────────────────────────────

def test_contiguous_runs_break_on_rest_stroke_change_and_gaps() -> None:
    lengths = (
        _lengths(2, 20.0)
        + [_rest(3, 30.0)]
        + _lengths(2, 20.0, start_index=4)
        + _lengths(2, 22.0, start_index=6, stroke="backstroke")
        + _lengths(1, 20.0, start_index=9)  # index gap 6→9 (missing records)
    )
    runs = contiguous_runs(lengths)
    assert [len(r) for r in runs] == [2, 2, 2, 1]


def test_best_continuous_effort_never_bridges_a_rest() -> None:
    # Two 50 m blocks separated by rest: a 100 m continuous effort does not exist.
    lengths = _lengths(2, 20.0) + [_rest(3, 60.0)] + _lengths(2, 18.0, start_index=4)
    assert best_continuous_effort(lengths, 100.0) is None
    best_50 = best_continuous_effort(lengths, 50.0)
    assert best_50["duration_s"] == 36.0  # the faster block
    assert best_50["pace_s_per_100m"] == 72.0
    assert best_50["lengths"] == 2
    assert "no rest bridged" in best_50["basis"]


def test_best_continuous_effort_requires_an_exact_distance() -> None:
    # A 33 m pool cannot produce an exact 100 m.
    lengths = _lengths(6, 30.0, distance=33.0)
    assert best_continuous_effort(lengths, 100.0) is None
    assert best_continuous_effort(lengths, 99.0) is not None


def test_continuous_bests_report_unsupported_distances(
) -> None:
    lengths = _lengths(4, 20.0)  # 100 m total in a 25 m pool
    bests = continuous_bests(lengths)
    assert bests["efforts"]["50m"]["duration_s"] == 40.0
    assert bests["efforts"]["100m"]["duration_s"] == 80.0
    assert "200m" in bests["unsupported"]
    assert "400m" in bests["unsupported"]


def test_no_length_data_means_no_continuous_bests() -> None:
    bests = continuous_bests([])
    assert bests["available"] is False
    assert "cannot be derived from whole-session averages" in bests["unavailable_reason"]


# ── Efficiency ─────────────────────────────────────────────────────────────────

def test_efficiency_prefers_length_records_over_session_averages() -> None:
    lengths = _lengths(4, 20.0, swolf=38.0, strokes=15)
    metrics = efficiency_metrics(lengths, {"avg_swolf": 99})
    assert metrics["avg_swolf"]["value"] == 38.0
    assert metrics["avg_swolf"]["source"] == "per-length records"
    assert metrics["avg_strokes_per_length"]["value"] == 15.0
    assert metrics["distance_per_stroke_m"]["value"] == pytest.approx(25 / 15, abs=0.01)


def test_efficiency_without_stroke_counts_is_unavailable_not_derived() -> None:
    lengths = _lengths(4, 20.0, swolf=None, strokes=None)
    metrics = efficiency_metrics(lengths, {})
    assert metrics["avg_swolf"] is None
    assert metrics["avg_strokes_per_length"] is None
    assert metrics["distance_per_stroke_m"] is None
    assert "cannot be derived from distance and time alone" in (
        metrics["efficiency_unavailable_reason"]
    )


def test_efficiency_groups_never_pool_different_pool_lengths() -> None:
    sessions = [
        build_session(
            {"activity_id": 1, "day": _day(3), "distance_m": 1000, "duration_s": 1500},
            {"pool_length_raw": 25, "pool_length_unit": "meter", "pool_length_m": 25.0,
             "primary_stroke": "freestyle", "status": "ok"},
            _lengths(4, 20.0, swolf=40.0),
        ),
        build_session(
            {"activity_id": 2, "day": _day(2), "distance_m": 1000, "duration_s": 1500},
            {"pool_length_raw": 50, "pool_length_unit": "meter", "pool_length_m": 50.0,
             "primary_stroke": "freestyle", "status": "ok"},
            _lengths(4, 45.0, distance=50.0, swolf=70.0),
        ),
    ]
    groups = efficiency_by_comparability(sessions)
    assert len(groups) == 2
    assert {g["pool_length_raw"] for g in groups} == {25, 50}
    assert all(g["session_count"] == 1 for g in groups)


# ── Sessions ───────────────────────────────────────────────────────────────────

def test_equal_distance_sessions_separate_active_speed_from_rest() -> None:
    """The headline acceptance criterion: same distance, different rest."""
    workout = {"activity_id": 1, "day": _day(2), "distance_m": 200.0,
               "duration_s": 400.0, "type": "lap_swimming"}
    long_rest = build_session(
        workout,
        {"status": "ok", "elapsed_duration_s": 400.0, "pool_length_m": 25.0,
         "pool_length_raw": 25, "pool_length_unit": "meter"},
        _lengths(4, 20.0) + [_rest(5, 160.0)] + _lengths(4, 20.0, start_index=6),
    )
    short_rest = build_session(
        {**workout, "activity_id": 2, "duration_s": 280.0},
        {"status": "ok", "elapsed_duration_s": 280.0, "pool_length_m": 25.0,
         "pool_length_raw": 25, "pool_length_unit": "meter"},
        _lengths(4, 20.0) + [_rest(5, 40.0)] + _lengths(4, 20.0, start_index=6),
    )
    # Identical active swimming, very different elapsed time.
    assert long_rest["active_pace_s_per_100m"] == short_rest["active_pace_s_per_100m"] == 80.0
    assert long_rest["rest_duration_s"] == 160.0
    assert short_rest["rest_duration_s"] == 40.0
    assert long_rest["elapsed_pace_s_per_100m"] > short_rest["elapsed_pace_s_per_100m"]


def test_elapsed_only_session_is_labelled_and_invents_nothing() -> None:
    session = build_session(
        {"activity_id": 3, "day": _day(1), "distance_m": 1000.0,
         "duration_s": 1800.0, "type": "lap_swimming"},
        None,
        None,
    )
    assert session["detail_status"] == "not_ingested"
    assert session["active_pace_s_per_100m"] is None
    assert session["active_pace_unavailable_reason"]
    assert session["elapsed_pace_s_per_100m"] == 180.0
    assert session["efficiency"]["avg_swolf"] is None
    assert session["continuous_bests"]["available"] is False


def test_mixed_stroke_session_reports_the_mix() -> None:
    lengths = _lengths(4, 20.0) + _lengths(4, 24.0, start_index=5, stroke="backstroke")
    session = build_session(
        {"activity_id": 4, "day": _day(1), "distance_m": 200.0, "duration_s": 200.0},
        {"status": "ok", "primary_stroke": "individual_medley"},
        lengths,
    )
    assert session["stroke_mix"] == {"backstroke": 4, "freestyle": 4}
    # A continuous 100 m exists in each stroke; the faster one wins.
    assert session["continuous_bests"]["efforts"]["100m"]["stroke"] == "freestyle"


# ── Window aggregation ─────────────────────────────────────────────────────────

def test_aggregate_keeps_active_and_elapsed_pace_apart() -> None:
    sessions = [
        {"activity_id": 1, "day": _day(3), "distance_m": 1000.0,
         "elapsed_duration_s": 1800.0, "active_duration_s": 1500.0,
         "rest_duration_s": 300.0, "avg_hr": 140},
        {"activity_id": 2, "day": _day(1), "distance_m": 1000.0,
         "elapsed_duration_s": 1700.0, "active_duration_s": 1500.0,
         "rest_duration_s": 200.0, "avg_hr": 142},
    ]
    agg = aggregate_sessions(sessions, window_days=14)
    assert agg["active_pace_s_per_100m"] == 150.0
    assert agg["elapsed_pace_s_per_100m"] == 175.0
    assert agg["avg_rest_duration_s"] == 250.0
    assert agg["active_pace_sample_count"] == 2
    assert agg["total_distance_m"] == 2000.0


def test_aggregate_without_active_time_reports_reason() -> None:
    agg = aggregate_sessions(
        [{"activity_id": 1, "day": _day(1), "distance_m": 1000.0,
          "elapsed_duration_s": 1800.0}],
        window_days=14,
    )
    assert agg["active_pace_s_per_100m"] is None
    assert "no session in this window recorded an active" in agg["active_pace_unavailable_reason"]
    assert agg["elapsed_pace_s_per_100m"] == 180.0


def test_window_bests_only_use_sessions_with_length_data() -> None:
    with_lengths = build_session(
        {"activity_id": 1, "day": _day(2), "distance_m": 100.0, "duration_s": 80.0},
        {"status": "ok"},
        _lengths(4, 20.0),
    )
    without = build_session(
        {"activity_id": 2, "day": _day(1), "distance_m": 100.0, "duration_s": 70.0},
        {"status": "ok"},
        [],
    )
    bests = window_continuous_bests([with_lengths, without])
    assert bests["sessions_with_length_data"] == 1
    assert bests["sessions_total"] == 2
    assert bests["efforts"]["100m"]["activity_id"] == 1


# ── End-to-end over the database ───────────────────────────────────────────────

def _store_swim(
    db: Database, activity_id: int, offset: int, *, distance: float, elapsed: float,
    active: float | None, lengths: list[dict] | None = None, pool: float | None = 25.0,
    detail: bool = True,
) -> None:
    db.upsert_workout(
        activity_id, _day(offset), name="Pool swim", type="lap_swimming",
        duration_s=elapsed, distance_m=distance, avg_hr=140,
        training_load=50, source="garmin", load_source="garmin",
    )
    if detail:
        db.upsert_activity_detail(
            activity_id, _day(offset), status="ok", activity_type="lap_swimming",
            elapsed_duration_s=elapsed, active_duration_s=active,
            pool_length_m=pool, pool_length_raw=pool, pool_length_unit="meter",
            primary_stroke="freestyle",
        )
    if lengths:
        db.replace_activity_lengths(activity_id, lengths)


def test_end_to_end_swimming_progress(db: Database) -> None:
    # Baseline: slower active swimming. Analysis window: faster.
    _store_swim(db, 10, 50, distance=1000, elapsed=1800, active=1600,
                lengths=_lengths(40, 40.0))
    _store_swim(db, 11, 43, distance=1000, elapsed=1800, active=1600,
                lengths=_lengths(40, 40.0))
    _store_swim(db, 20, 10, distance=1000, elapsed=1700, active=1500,
                lengths=_lengths(40, 37.5))
    _store_swim(db, 21, 3, distance=1000, elapsed=1700, active=1500,
                lengths=_lengths(40, 37.5))

    report = build_swimming_progress(db, days=30)
    assert report["available"] is True
    assert report["summary"]["session_count"] == 2
    assert report["summary"]["active_pace_s_per_100m"] == 150.0
    assert report["baseline_summary"]["active_pace_s_per_100m"] == 160.0
    assert report["comparison"]["active_pace_s_per_100m"]["improved"] is True
    assert report["comparison"]["active_pace_s_per_100m"]["absolute_change"] == -10.0
    assert report["comparison"]["elapsed_pace_s_per_100m"]["improved"] is True
    assert report["continuous_bests"]["efforts"]["100m"]["duration_s"] == 150.0
    assert "faster swimming, shorter rests or greater effort" in (
        report["comparison"]["interpretation_note"]
    )


def test_progress_flags_elapsed_only_sessions(db: Database) -> None:
    _store_swim(db, 30, 5, distance=1000, elapsed=1800, active=None)
    report = build_swimming_progress(db, days=30)
    assert report["summary"]["active_pace_s_per_100m"] is None
    assert 30 in report["data_quality"]["elapsed_time_only_sessions"]
    assert report["summary"]["elapsed_pace_s_per_100m"] == 180.0
    assert any("elapsed-time-only" in n for n in report["data_quality"]["notes"])


def test_progress_reports_sessions_without_ingested_detail(db: Database) -> None:
    _store_swim(db, 40, 5, distance=1000, elapsed=1800, active=None, detail=False)
    report = build_swimming_progress(db, days=30)
    assert report["data_quality"]["sessions_without_detail_ingested"] == [40]
    assert any("backfill_swim_details" in n for n in report["data_quality"]["notes"])


def test_filters_report_what_they_excluded(db: Database) -> None:
    _store_swim(db, 50, 5, distance=1000, elapsed=1800, active=1500,
                lengths=_lengths(40, 37.5), pool=25.0)
    _store_swim(db, 51, 4, distance=1000, elapsed=1800, active=1500, pool=50.0)
    report = build_swimming_progress(db, days=30, pool_length_m=25.0)
    assert report["summary"]["session_count"] == 1
    assert [e["activity_id"] for e in report["filters"]["excluded_by_filter"]] == [51]
    assert report["filters"]["pool_length_m"] == 25.0


def test_open_water_and_pool_reported_separately(db: Database) -> None:
    _store_swim(db, 60, 5, distance=1000, elapsed=1800, active=1500)
    db.upsert_workout(
        61, _day(4), name="Sea swim", type="open_water_swimming",
        duration_s=2400, distance_m=1500, source="garmin",
    )
    report = build_swimming_progress(db, days=30)
    assert set(report["by_modality"]) == {"lap_swimming", "open_water_swimming"}
    assert report["by_modality"]["open_water_swimming"]["session_count"] == 1


def test_duplicate_swim_recorded_twice_counts_once(db: Database) -> None:
    _store_swim(db, 70, 5, distance=1000, elapsed=1800, active=1500)
    db.upsert_workout(
        71, _day(5), name="Swim (Apple)", type="lap_swimming",
        duration_s=1800, distance_m=1000, source="apple", load_source="estimated",
    )
    db.dedupe_workouts(days=30)
    report = build_swimming_progress(db, days=30)
    assert report["summary"]["session_count"] == 1
    assert report["summary"]["total_distance_m"] == 1000.0


def test_no_swims_returns_safe_empty_report(db: Database) -> None:
    report = build_swimming_progress(db, days=30)
    assert report["available"] is False
    assert report["summary"]["session_count"] == 0
    assert report["summary"]["total_distance_m"] == 0.0
    assert report["continuous_bests"]["available"] is False


def test_zero_distance_and_zero_duration_are_safe(db: Database) -> None:
    db.upsert_workout(
        80, _day(2), name="Broken swim", type="lap_swimming",
        duration_s=0, distance_m=0, source="garmin",
    )
    report = build_swimming_progress(db, days=30)
    assert report["summary"]["session_count"] == 1
    assert report["summary"]["active_pace_s_per_100m"] is None
    assert 80 in report["data_quality"]["zero_distance_sessions"]


# ── Ingestion ──────────────────────────────────────────────────────────────────

class FakeSwimApi:
    """Canned Garmin payloads for one pool swim, mirroring real shapes."""

    def __init__(self) -> None:
        self.detail_calls: list[int] = []

    def get_activities_by_date(self, start, end):
        return [{
            "activityId": 900,
            "activityName": "Pool swim",
            "activityType": {"typeKey": "lap_swimming"},
            "duration": 1800.0,
            "distance": 1000.0,
            "calories": 320,
            "averageHR": 138,
            "maxHR": 155,
            "startTimeLocal": f"{start} 07:10:00",
        }]

    def get_activity(self, activity_id):
        self.detail_calls.append(activity_id)
        return {
            "summaryDTO": {
                "duration": 1750.0,
                "elapsedDuration": 1800.0,
                "movingDuration": 1500.0,
                "poolLength": 25.0,
                "numberOfActiveLengths": 40,
                "strokes": 620,
                "averageSwolf": 42.0,
                "avgStrokeDistance": 1.6,
                "swimStroke": "FREESTYLE",
            },
            "unitOfPoolLength": {"unitKey": "meter"},
        }

    def get_activity_typed_splits(self, activity_id):
        return {
            "lengthDTOs": [
                {
                    "lengthIndex": i + 1, "duration": 37.5, "distance": 25.0,
                    "swimStroke": "FREESTYLE", "totalStrokes": 15, "swolf": 42,
                    "lengthType": "ACTIVE",
                }
                for i in range(40)
            ] + [
                {"lengthIndex": 41, "duration": 60.0, "distance": 0.0,
                 "lengthType": "INTERVAL_REST"}
            ]
        }


@pytest.fixture()
def swim_client(db: Database, monkeypatch) -> GarminClient:
    monkeypatch.setattr(garmin_client, "PULL_PAUSE_SECONDS", 0)
    client = GarminClient(db=db)
    client.api = FakeSwimApi()
    return client


def test_swim_detail_ingested_with_the_summary(swim_client: GarminClient, db: Database) -> None:
    swim_client._pull_workouts(_day(1))
    detail = db.activity_details([900])[900]
    assert detail["status"] == "ok"
    assert detail["elapsed_duration_s"] == 1800.0
    assert detail["active_duration_s"] == 1500.0
    assert detail["timer_duration_s"] == 1750.0
    assert detail["pool_length_m"] == 25.0
    assert detail["pool_length_unit"] == "meter"
    lengths = db.activity_lengths([900])[900]
    assert len(lengths) == 41
    assert sum(1 for l in lengths if l["is_rest"]) == 1


def test_detail_ingestion_is_idempotent(swim_client: GarminClient, db: Database) -> None:
    swim_client._pull_workouts(_day(1))
    swim_client._pull_workouts(_day(1))
    assert len(db.activity_lengths([900])[900]) == 41  # replaced, not appended
    assert len(db.recent_workouts(days=7)) == 1


def test_detail_failure_never_breaks_summary_ingestion(
    swim_client: GarminClient, db: Database
) -> None:
    def boom(activity_id):
        raise RuntimeError("provider exploded")

    swim_client.api.get_activity = boom
    swim_client.api.get_activity_typed_splits = boom
    swim_client._pull_workouts(_day(1))
    # The workout itself still landed; the detail row records the failure.
    assert len(db.recent_workouts(days=7)) == 1
    assert db.activity_details([900])[900]["status"] == "error"


def test_backfill_is_bounded_and_resumable(swim_client: GarminClient, db: Database) -> None:
    for i in range(5):
        db.upsert_workout(
            500 + i, _day(i + 1), name="Old swim", type="lap_swimming",
            duration_s=1800, distance_m=1000, source="garmin",
        )
    first = swim_client.pull_activity_details(days=30, limit=2)
    assert len(first["processed"]) == 2
    assert first["remaining"] == 3
    second = swim_client.pull_activity_details(days=30, limit=2)
    # Resumed: it moved on to the next two, it did not redo the first two.
    assert {p["activity_id"] for p in first["processed"]} & {
        p["activity_id"] for p in second["processed"]
    } == set()
    assert second["remaining"] == 1


def test_backfill_skips_non_swim_activities(swim_client: GarminClient, db: Database) -> None:
    db.upsert_workout(600, _day(1), name="Run", type="running", duration_s=1800)
    result = swim_client.pull_activity_details(days=30, limit=10)
    assert result["processed"] == []
    assert result["remaining"] == 0


def test_unsupported_provider_is_recorded_not_retried_forever(
    db: Database, monkeypatch
) -> None:
    monkeypatch.setattr(garmin_client, "PULL_PAUSE_SECONDS", 0)

    class NoDetailApi:
        pass

    db.upsert_workout(
        700, _day(1), name="Swim", type="lap_swimming", duration_s=1800, distance_m=1000
    )
    client = GarminClient(db=db)
    client.api = NoDetailApi()
    result = client.pull_activity_details(days=30, limit=5)
    assert result["status_counts"] == {"unsupported": 1}
    assert db.activity_details([700])[700]["status"] == "unsupported"
    # A second run does not ask again.
    assert client.pull_activity_details(days=30, limit=5)["processed"] == []


def test_generic_activity_progress_uses_the_same_active_clock(db: Database) -> None:
    """Swim totals must agree between the generic and specialist services."""
    from garmin_coach.activity_progress import build_activity_progress

    _store_swim(db, 90, 5, distance=1000, elapsed=1800, active=1500,
                lengths=_lengths(40, 37.5))
    generic = build_activity_progress(db, "lap_swimming", days=30)
    specialist = build_swimming_progress(db, days=30)

    assert generic["summary"]["time_basis"] == "active_duration"
    # 1500 s over 1000 m = 150 s / 100 m in both views.
    assert generic["summary"]["pace"]["seconds_per_unit"] == 150.0
    assert specialist["summary"]["active_pace_s_per_100m"] == 150.0
    assert generic["summary"]["workout_count"] == specialist["summary"]["session_count"]
    assert generic["summary"]["total_distance_m"] == specialist["summary"]["total_distance_m"]
