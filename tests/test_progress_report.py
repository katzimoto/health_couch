"""Tests for the unified training progress report (issue #11).

Offline over a synthetic mixed-sport dataset that always includes swimming,
strength and every other recorded type — the report's central promise is that
nothing gets silently omitted.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_coach.activity_progress import build_activity_progress
from garmin_coach.database import Database
from garmin_coach.progress_report import (
    adherence_summary,
    build_training_progress_report,
    collect_concerns,
    progress_report_summary,
    recovery_context,
)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "report.db"))


def _day(offset: int) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


def _mixed_dataset(db: Database) -> None:
    """Swimming, running, cycling, walking and strength, over two windows."""
    # Analysis window (last 28 days).
    for i, offset in enumerate((2, 9, 16, 23)):
        db.upsert_workout(
            100 + i, _day(offset), name="Run", type="running", duration_s=1500,
            distance_m=5000, avg_hr=150, training_load=70,
            source="garmin", load_source="garmin",
        )
    for i, offset in enumerate((4, 11, 18)):
        db.upsert_workout(
            200 + i, _day(offset), name="Swim", type="lap_swimming", duration_s=1800,
            distance_m=1500, avg_hr=135, training_load=45,
            source="garmin", load_source="garmin",
        )
        db.upsert_activity_detail(
            200 + i, _day(offset), status="ok", activity_type="lap_swimming",
            elapsed_duration_s=1800, active_duration_s=1500,
            pool_length_m=25.0, pool_length_raw=25, pool_length_unit="meter",
            primary_stroke="freestyle",
        )
    db.upsert_workout(
        300, _day(6), name="Ride", type="cycling", duration_s=3600,
        distance_m=30000, avg_hr=130, training_load=60,
        source="garmin", load_source="garmin",
    )
    db.upsert_workout(
        310, _day(8), name="Walk", type="walking", duration_s=2400,
        distance_m=3000, training_load=20, source="garmin", load_source="estimated",
    )
    for offset in (3, 10, 17):
        db.add_strength_session(
            _day(offset), duration_s=3600,
            exercises=[{
                "exercise_name": "bench press",
                "actual_sets": [{"reps": 8, "weight_kg": 60 + offset // 10, "rpe": 7}] * 3,
            }],
        )

    # Baseline window (29–56 days ago): slower running, fewer swims.
    for i, offset in enumerate((32, 39, 46)):
        db.upsert_workout(
            400 + i, _day(offset), name="Run", type="running", duration_s=1800,
            distance_m=5000, avg_hr=148, training_load=60,
            source="garmin", load_source="garmin",
        )
    db.upsert_workout(
        410, _day(35), name="Swim", type="lap_swimming", duration_s=2000,
        distance_m=1500, avg_hr=134, training_load=42,
        source="garmin", load_source="garmin",
    )


# ── Adherence ──────────────────────────────────────────────────────────────────

def test_no_plan_means_unknown_not_zero() -> None:
    result = adherence_summary([])
    assert result["available"] is False
    assert "unknown, not zero" in result["unavailable_reason"]
    assert "completion_rate_pct" not in result


def test_adherence_counts_partial_and_skipped_with_a_denominator() -> None:
    result = adherence_summary([
        {"id": 1, "day": _day(5), "status": "done"},
        {"id": 2, "day": _day(4), "status": "done"},
        {"id": 3, "day": _day(3), "status": "partially_done"},
        {"id": 4, "day": _day(2), "status": "skipped", "skip_reason": "illness"},
        {"id": 5, "day": _day(0), "status": "planned"},  # not resolved yet
    ])
    assert result["planned_sessions"] == 5
    assert result["denominator"] == 4          # the open plan is excluded
    assert result["still_open"] == 1
    assert result["completed"] == 2
    assert result["partially_completed"] == 1
    assert result["skipped"] == 1
    # (2 + 0.5) / 4 = 62.5%
    assert result["completion_rate_pct"] == 62.5
    assert result["skip_reasons"][0]["reason"] == "illness"


def test_adherence_rate_withheld_until_something_resolves() -> None:
    result = adherence_summary([{"id": 1, "day": _day(0), "status": "planned"}])
    assert result["available"] is True
    assert result["completion_rate_pct"] is None
    assert "no plan in the window has been resolved" in result["rate_unavailable_reason"]


# ── Recovery is context, not a cause ───────────────────────────────────────────

def test_recovery_is_reused_and_labelled_as_context() -> None:
    context = recovery_context(
        {
            "available": True, "as_of": _day(0),
            "trends": {"sleep_hours": {"avg_7d": 7.1}, "hrv": {"avg_7d": 60}},
            "sleep_debt_7d": 2.0, "sleep_target_hours": 7.0, "flags": [],
        },
        {"day": _day(0), "energy_1_10": 7},
    )
    assert context["available"] is True
    assert "Analyzer.report" in context["source"]
    assert "not as a cause" in context["interpretation_note"]


def test_recovery_unavailable_is_explicit() -> None:
    context = recovery_context({"available": False, "reason": "No data yet"}, None)
    assert context["available"] is False
    assert context["unavailable_reason"] == "No data yet"


# ── The report ─────────────────────────────────────────────────────────────────

def test_every_recorded_sport_gets_a_section(db: Database) -> None:
    _mixed_dataset(db)
    report = build_training_progress_report(db, days=28)
    covered = set(report["sports"]["activity_types_covered"])
    # Swimming in particular must never be missing.
    assert {"running", "lap_swimming", "cycling", "walking", "strength_training"} <= covered
    assert report["sports"]["available"] is True
    assert report["swimming"]["available"] is True


def test_sections_agree_exactly_with_the_per_sport_endpoint(db: Database) -> None:
    """Totals must match get_activity_progress for the same window."""
    _mixed_dataset(db)
    report = build_training_progress_report(db, days=28)
    running = next(
        s for s in report["sports"]["sections"] if s["activity_type"] == "running"
    )
    endpoint = build_activity_progress(db, "running", days=28)
    assert running["summary"]["workout_count"] == endpoint["summary"]["workout_count"]
    assert running["summary"]["total_distance_m"] == endpoint["summary"]["total_distance_m"]
    assert (
        running["summary"]["pace"]["seconds_per_unit"]
        == endpoint["summary"]["pace"]["seconds_per_unit"]
    )
    assert running["records"] == endpoint["records"]


def test_windows_and_baseline_selection_are_explicit(db: Database) -> None:
    _mixed_dataset(db)
    report = build_training_progress_report(db, days=28)
    assert report["analysis_window"]["days"] == 28
    assert report["comparison_window"]["days"] == 28
    assert report["comparison_window"]["end"] < report["analysis_window"]["start"]
    assert "own documented baseline" in report["comparison_window"]["selection_method"]
    assert report["coverage"]["window_days"] == 28
    assert report["as_of"] == date.today().isoformat()


def test_active_pace_gains_are_distinguished_from_rest_and_effort(db: Database) -> None:
    _mixed_dataset(db)
    report = build_training_progress_report(db, days=28)
    running = next(
        s for s in report["sports"]["sections"] if s["activity_type"] == "running"
    )
    pace = running["comparison"]["pace"]
    assert pace["improved"] is True           # 25 min vs 30 min over 5 km
    assert "higher heart rate" in pace["caveat"]
    # Swimming reports the active clock separately from the elapsed one.
    assert report["swimming"]["summary"]["active_pace_s_per_100m"] == 100.0
    assert report["swimming"]["summary"]["elapsed_pace_s_per_100m"] == 120.0


def test_strength_keeps_its_own_conventions(db: Database) -> None:
    _mixed_dataset(db)
    report = build_training_progress_report(db, days=28)
    exercises = {e["exercise"]: e for e in report["strength"]["exercises"]}
    assert "bench press" in exercises
    assert exercises["bench press"]["load_convention"] == "total_load"
    assert exercises["bench press"]["heaviest_weight_kg"]["value"] is not None
    # No distance or pace is invented for strength.
    strength_section = next(
        s for s in report["sports"]["sections"]
        if s["activity_type"] == "strength_training"
    )
    assert strength_section["summary"]["total_distance_m"] is None
    assert strength_section["summary"]["pace"] is None
    assert strength_section["specialist_tool"] == "get_strength_progress"


def test_prs_are_labelled_observation_or_estimate(db: Database) -> None:
    _mixed_dataset(db)
    report = build_training_progress_report(db, days=28)
    prs = report["notable_prs"]
    assert prs
    assert all(pr["claim_type"] in ("observation", "estimate") for pr in prs)
    assert all(pr.get("basis") for pr in prs)
    e1rm = [pr for pr in prs if pr["kind"] == "estimated_1rm"]
    assert e1rm and e1rm[0]["claim_type"] == "estimate"
    assert "not a measured maximum" in e1rm[0]["basis"]
    # Every PR is traceable to its source record.
    assert all(pr.get("activity_id") or pr.get("session_id") for pr in prs)


def test_no_blended_overall_fitness_number(db: Database) -> None:
    _mixed_dataset(db)
    report = build_training_progress_report(db, days=28)
    assert "overall_fitness" not in report
    assert any("overall fitness" in rule for rule in report["interpretation_rules"])


def test_concerns_surface_alongside_positive_change(db: Database) -> None:
    _mixed_dataset(db)
    report = build_training_progress_report(db, days=28)
    kinds = {c["type"] for c in report["concerns"]}
    # No plans were recorded, and several sports have thin samples.
    assert "adherence_unknown" in kinds
    assert "thin_sample" in kinds


def test_effort_confounded_improvement_is_called_out() -> None:
    sports = [{
        "activity_type": "running",
        "summary": {"workout_count": 8},
        "comparison": {
            "pace": {"improved": True, "absolute_change_s": -20.0,
                     "unit": "min_per_km"},
            "avg_hr": {"absolute_change": 9.0},
            "weekly_duration_s": {"percent_change": 5.0},
        },
    }]
    concerns = collect_concerns(
        sports, {"available": True}, {"by_type": {}}, {"coverage_ratio": 1.0}
    )
    assert any(c["type"] == "effort_confounded_improvement" for c in concerns)
    detail = next(
        c for c in concerns if c["type"] == "effort_confounded_improvement"
    )["detail"]
    assert "not demonstrated aerobic improvement" in detail


def test_incomplete_recordings_are_reported_and_excluded(db: Database) -> None:
    _mixed_dataset(db)
    db.upsert_workout(
        500, _day(5), name="Glitch", type="running", duration_s=600,
        distance_m=9000, training_load=200, source="garmin", load_source="garmin",
    )
    report = build_training_progress_report(db, days=28)
    assert 500 in report["data_quality"]["excluded_from_totals_and_records"]
    running = next(
        s for s in report["sports"]["sections"] if s["activity_type"] == "running"
    )
    assert running["summary"]["workout_count"] == 4  # the glitch is not counted
    assert any(c["type"] == "impossible_speed" for c in report["concerns"])


def test_sparse_history_gives_partial_sections_not_invented_statistics(db: Database) -> None:
    db.upsert_workout(
        600, _day(2), name="Only run", type="running", duration_s=1500,
        distance_m=5000, source="garmin",
    )
    report = build_training_progress_report(db, days=28)
    running = next(
        s for s in report["sports"]["sections"] if s["activity_type"] == "running"
    )
    assert running["summary"]["workout_count"] == 1
    assert running["comparison"]["weekly_distance_m"]["percent_change"] is None
    assert running["baseline_note"]
    assert report["adherence"]["available"] is False
    assert report["strength"]["available"] is False


def test_empty_database_is_safe(db: Database) -> None:
    report = build_training_progress_report(db, days=28)
    assert report["sports"]["available"] is False
    assert report["swimming"]["available"] is False
    assert report["strength"]["available"] is False
    assert report["notable_prs"] == []
    assert report["data_quality"]["finding_count"] == 0


def test_detail_is_opt_in(db: Database) -> None:
    _mixed_dataset(db)
    summary = build_training_progress_report(db, days=28)
    assert "sport_detail" not in summary
    assert summary["detail_included"] is False

    detailed = build_training_progress_report(db, days=28, detail=True)
    assert "running" in detailed["sport_detail"]
    assert "weekly_series" in detailed["sport_detail"]["running"]
    assert "bench press" in detailed["strength_detail"]
    assert detailed["training_load_by_sport"]["reconciliation"]["reconciles"] is True


def test_merged_sessions_counted_once(db: Database) -> None:
    db.upsert_workout(
        700, _day(3), name="Run (Garmin)", type="running", duration_s=1500,
        distance_m=5000, training_load=70, source="garmin", load_source="garmin",
    )
    db.upsert_workout(
        701, _day(3), name="Run (Apple)", type="running", duration_s=1500,
        distance_m=5000, training_load=65, source="apple", load_source="estimated",
    )
    db.dedupe_workouts(days=28)
    report = build_training_progress_report(db, days=28)
    running = next(
        s for s in report["sports"]["sections"] if s["activity_type"] == "running"
    )
    assert running["summary"]["workout_count"] == 1
    assert running["summary"]["total_distance_m"] == 5000.0


# ── Reuse by the coaching context ──────────────────────────────────────────────

def test_summary_is_bounded_and_carries_the_caveats(db: Database) -> None:
    _mixed_dataset(db)
    summary = progress_report_summary(build_training_progress_report(db, days=28))
    assert summary["source"] == "get_training_progress_report"
    assert summary["analysis_window"]["days"] == 28
    assert len(summary["notable_prs"]) <= 5
    assert len(summary["strength"]) <= 6
    assert summary["interpretation_rules"]


def test_coaching_context_can_include_the_progress_summary(db: Database) -> None:
    from garmin_coach.coaching_context import build_coaching_context

    _mixed_dataset(db)
    without = build_coaching_context(db, refresh_if_stale=False)
    assert "progress_summary" not in without

    with_summary = build_coaching_context(
        db, refresh_if_stale=False, include_progress_summary=True
    )
    assert with_summary["progress_summary"]["source"] == "get_training_progress_report"
    assert with_summary["progress_summary"]["sports"]
    # The rest of the context is unchanged.
    assert with_summary["recovery"]["status"] == without["recovery"]["status"]


def test_existing_full_report_consumers_are_unaffected(db: Database) -> None:
    """get_full_report keeps its shape — the new report is additive."""
    import garmin_coach.mcp_server as mcp_server

    _mixed_dataset(db)
    report = mcp_server.get_full_report()
    assert {"available", "merged_workouts"} <= set(report)
