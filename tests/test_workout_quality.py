"""Tests for workout data-quality detection and quality-aware merging (issue #9).

The motivating case is the synthetic one from the issue: a wearable "strength"
activity lasting a few seconds alongside a detailed, much longer manual log of
the same session. Everything here is offline over synthetic fixtures.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_coach.coaching_context import detect_workout_quality_warnings
from garmin_coach.database import Database
from garmin_coach.workout_merge import coverage_annotations, merge_fields
from garmin_coach.workout_quality import (
    NEAR_ZERO_DURATION_S,
    detect_findings,
    findings_to_warnings,
    quality_report,
    record_blocking_ids,
    source_quality,
)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "quality.db"))


def _day(offset: int = 1) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


def _types(findings: list[dict]) -> set[str]:
    return {f["type"] for f in findings}


# ── Single-row detection ───────────────────────────────────────────────────────

def test_near_zero_duration_is_flagged() -> None:
    findings = detect_findings([{
        "activity_id": 1, "day": _day(), "type": "strength_training",
        "duration_s": 11, "source": "garmin",
    }])
    assert "near_zero_duration" in _types(findings)
    finding = next(f for f in findings if f["type"] == "near_zero_duration")
    assert finding["evidence"]["duration_s"] == 11
    assert finding["evidence"]["threshold_s"] == NEAR_ZERO_DURATION_S
    assert finding["blocks_records"] is True
    assert "slice" in finding["suggested_action"]


def test_missing_essential_data_is_flagged_once() -> None:
    findings = detect_findings([{
        "activity_id": 2, "day": _day(), "type": "running",
        "duration_s": None, "distance_m": None, "source": "manual",
    }])
    assert _types(findings) == {"missing_essential_data"}
    assert findings[0]["blocks_records"] is True


def test_impossible_speed_and_zero_distance_still_detected() -> None:
    findings = detect_findings([
        {"activity_id": 3, "day": _day(), "type": "running",
         "duration_s": 1800, "distance_m": 0, "source": "garmin"},
        {"activity_id": 4, "day": _day(), "type": "running",
         "duration_s": 600, "distance_m": 9000, "source": "garmin"},
    ])
    assert "zero_distance" in _types(findings)
    assert "impossible_speed" in _types(findings)
    speed = next(f for f in findings if f["type"] == "impossible_speed")
    assert speed["severity"] == "critical"
    assert speed["evidence"]["avg_speed_kmh"] == 54.0


def test_distance_without_duration_is_flagged() -> None:
    findings = detect_findings([{
        "activity_id": 5, "day": _day(), "type": "cycling",
        "duration_s": 0, "distance_m": 20000, "source": "apple",
    }])
    assert "impossible_time_distance" in _types(findings)


def test_cycling_is_exempt_from_the_running_speed_threshold() -> None:
    findings = detect_findings([{
        "activity_id": 6, "day": _day(), "type": "cycling",
        "duration_s": 3600, "distance_m": 50000, "source": "garmin",
    }])
    assert "impossible_speed" not in _types(findings)


def test_strength_without_distance_is_not_flagged() -> None:
    findings = detect_findings([{
        "activity_id": 7, "day": _day(), "type": "strength_training",
        "duration_s": 3000, "distance_m": None, "source": "manual",
    }])
    assert findings == []


# ── The headline case: a truncated device recording ────────────────────────────

def _truncated_pair() -> list[dict]:
    """A few-second watch recording plus a detailed 55-minute manual session."""
    return [
        {"activity_id": 100, "day": _day(2), "type": "strength_training",
         "name": "Strength", "duration_s": 11, "avg_hr": 92, "max_hr": 99,
         "calories": 3, "training_load": 1.0, "source": "garmin",
         "load_source": "garmin", "start_time": f"{_day(2)} 18:00:00"},
        {"activity_id": -100, "day": _day(2), "type": "strength_training",
         "name": "Push day", "duration_s": 3300, "calories": 320,
         "training_load": 75.0, "source": "manual", "load_source": "estimated",
         "start_time": f"{_day(2)} 18:00:00"},
    ]


def test_incomplete_recording_is_detected_with_evidence() -> None:
    findings = detect_findings(_truncated_pair(), include_all_sources=True)
    partial = next(f for f in findings if f["type"] == "partial_recording")
    assert partial["activity_id"] == 100  # the short device recording
    assert partial["evidence"]["recorded_duration_s"] == 11
    assert partial["evidence"]["other_source_duration_s"] == 3300
    assert partial["evidence"]["coverage_ratio"] < 0.01
    assert partial["severity"] == "critical"
    assert "partial coverage" in partial["suggested_action"]
    assert 100 in record_blocking_ids(findings)


def test_source_quality_marks_the_short_recording_incomplete() -> None:
    manual, garmin = _truncated_pair()[1], _truncated_pair()[0]
    quality = source_quality({"manual": manual, "garmin": garmin})
    assert quality["manual"]["complete"] is True
    assert quality["garmin"]["complete"] is False
    assert quality["garmin"]["coverage_ratio"] == pytest.approx(0.003, abs=0.001)
    assert "covers" in quality["garmin"]["reason"]


def test_quality_aware_merge_keeps_the_manual_duration_and_load() -> None:
    garmin, manual = _truncated_pair()
    sources = {"manual": manual, "garmin": garmin}
    quality = source_quality(sources)

    # Without quality awareness Garmin wins duration/calories/load by priority.
    naive, naive_provenance = merge_fields(sources, is_strength=True)
    assert naive["duration_s"] == 11
    assert naive_provenance["duration_s"] == "garmin"

    # With it, the demonstrably incomplete recording cannot supply them.
    merged, provenance = merge_fields(sources, is_strength=True, quality=quality)
    assert merged["duration_s"] == 3300
    assert provenance["duration_s"] == "manual"
    assert merged["calories"] == 320
    assert merged["training_load"] == 75.0
    # Partial physiology is preserved, with its coverage recorded.
    assert merged["avg_hr"] == 92
    coverage = coverage_annotations(provenance, quality)
    assert coverage["avg_hr"]["covers"] == "partial"
    assert coverage["avg_hr"]["coverage_ratio"] < 0.01
    assert "duration_s" not in coverage  # manual supplied it, and it is complete


def test_complete_garmin_recording_still_wins_physiology() -> None:
    """Quality awareness must not undo the normal priority rules."""
    garmin = {"activity_id": 200, "day": _day(), "type": "strength_training",
              "duration_s": 3200, "avg_hr": 128, "calories": 400,
              "training_load": 90.0, "source": "garmin", "load_source": "garmin"}
    manual = {"activity_id": -200, "day": _day(), "type": "strength_training",
              "duration_s": 3300, "calories": 350, "training_load": 70.0,
              "source": "manual", "load_source": "estimated"}
    sources = {"manual": manual, "garmin": garmin}
    merged, provenance = merge_fields(
        sources, is_strength=True, quality=source_quality(sources)
    )
    assert provenance["duration_s"] == "garmin"
    assert provenance["calories"] == "garmin"
    assert merged["training_load"] == 90.0
    assert provenance["exercise_details"] == "manual"
    assert coverage_annotations(provenance, source_quality(sources)) == {}


# ── Ambiguity stays unresolved ─────────────────────────────────────────────────

def test_two_legitimate_same_day_sessions_are_not_merged_on_date_alone() -> None:
    morning = {"activity_id": 300, "day": _day(), "type": "running",
               "duration_s": 1800, "distance_m": 5000, "source": "garmin",
               "start_time": f"{_day()} 07:00:00"}
    evening = {"activity_id": -300, "day": _day(), "type": "running",
               "duration_s": 2400, "distance_m": 7000, "source": "manual",
               "start_time": f"{_day()} 19:00:00"}
    findings = detect_findings([morning, evening], include_all_sources=True)
    candidate = next(f for f in findings if f["type"] == "unresolved_match_candidate")
    assert candidate["evidence"]["match_confidence"] < 0.5
    assert "12" in candidate["evidence"]["match_evidence"]  # 12h apart
    assert "left" in candidate["suggested_action"]
    # Detection only: nothing here merges them.
    assert candidate["severity"] == "info"


def test_already_linked_pair_is_not_reported_as_unresolved() -> None:
    garmin, manual = _truncated_pair()
    links = [
        {"canonical_activity_id": 999, "source_activity_id": 100, "source": "garmin"},
        {"canonical_activity_id": 999, "source_activity_id": -100, "source": "manual"},
    ]
    findings = detect_findings([garmin, manual], include_all_sources=True, links=links)
    assert "unresolved_match_candidate" not in _types(findings)
    assert "partial_recording" in _types(findings)  # the quality problem remains


# ── One pipeline ───────────────────────────────────────────────────────────────

def test_legacy_warning_shape_is_preserved() -> None:
    """The coaching context's existing consumers must keep working."""
    warnings = detect_workout_quality_warnings([
        {"activity_id": 1, "type": "running", "duration_s": 1800, "distance_m": 0},
        {"activity_id": 2, "type": "running", "duration_s": 600, "distance_m": 9000},
        {"activity_id": 3, "type": "strength_training", "duration_s": 3000,
         "distance_m": None},
    ])
    fields = {(w["activity_id"], w["field"]) for w in warnings}
    assert (1, "distance_m") in fields
    assert any(w["activity_id"] == 2 for w in warnings)
    assert not any(w["activity_id"] == 3 for w in warnings)
    assert all({"activity_id", "field", "status", "reason", "action"} <= set(w)
               for w in warnings)
    assert next(w for w in warnings if w["activity_id"] == 1)["action"] == (
        "excluded_from_pace_calcs"
    )


def test_findings_to_warnings_skips_informational_findings() -> None:
    findings = detect_findings([
        {"activity_id": 300, "day": _day(), "type": "running", "duration_s": 1800,
         "distance_m": 5000, "source": "garmin", "start_time": f"{_day()} 07:00:00"},
        {"activity_id": -300, "day": _day(), "type": "running", "duration_s": 2400,
         "distance_m": 7000, "source": "manual", "start_time": f"{_day()} 19:00:00"},
    ], include_all_sources=True)
    assert "unresolved_match_candidate" in _types(findings)
    # An unresolved candidate is not a "suspicious field" — it isn't a warning.
    assert findings_to_warnings(findings) == []


# ── The report and its consumers ───────────────────────────────────────────────

def test_quality_report_rolls_up_and_stays_read_only(db: Database) -> None:
    garmin, manual = _truncated_pair()
    db.upsert_workout(garmin["activity_id"], garmin["day"], **{
        k: v for k, v in garmin.items() if k not in ("activity_id", "day")
    })
    db.upsert_workout(manual["activity_id"], manual["day"], **{
        k: v for k, v in manual.items() if k not in ("activity_id", "day")
    })
    before = db.recent_workouts(days=30, include_duplicates=True)

    report = db.workout_data_quality(days=30)
    assert report["read_only"] is True
    assert report["workouts_examined"] == 2
    assert report["by_type"]["partial_recording"] == 1
    assert report["by_severity"]["critical"] >= 1
    assert 100 in report["records_blocked_activity_ids"]
    assert report["thresholds"]["near_zero_duration_s"] == NEAR_ZERO_DURATION_S

    after = db.recent_workouts(days=30, include_duplicates=True)
    assert before == after  # storage untouched


def test_report_uses_source_rows_not_only_canonicals(db: Database) -> None:
    """The check needs both sides of a merge, so duplicates are read too."""
    garmin, manual = _truncated_pair()
    for row in (garmin, manual):
        db.upsert_workout(row["activity_id"], row["day"], **{
            k: v for k, v in row.items() if k not in ("activity_id", "day")
        })
    merged = db.merge_workout_sources(source_activity_ids=[100, -100])
    assert merged["merged"] is True
    report = db.workout_data_quality(days=30)
    assert report["by_type"]["partial_recording"] == 1
    # The merge itself is already resolved, so no unresolved candidate remains.
    assert "unresolved_match_candidate" not in report["by_type"]


def test_merge_through_the_database_prefers_the_complete_source(db: Database) -> None:
    garmin, manual = _truncated_pair()
    for row in (garmin, manual):
        db.upsert_workout(row["activity_id"], row["day"], **{
            k: v for k, v in row.items() if k not in ("activity_id", "day")
        })
    result = db.merge_workout_sources(source_activity_ids=[100, -100])
    merge = result["merges"][0]
    assert merge["duration_s"] == 3300
    assert merge["field_sources"]["duration_s"] == "manual"
    assert merge["field_coverage"]["avg_hr"]["covers"] == "partial"
    assert merge["source_quality"]["garmin"]["complete"] is False

    canonical = db.get_merged_workout(merge["canonical_activity_id"])
    assert canonical["physiology"]["duration_s"] == 3300
    # Partial physiology is preserved, not discarded — and labelled as partial.
    assert canonical["physiology"]["avg_hr"] == 92
    assert canonical["field_coverage"]["avg_hr"]["covers"] == "partial"
    assert canonical["field_sources"]["duration_s"] == "manual"


def test_dry_run_changes_nothing(db: Database) -> None:
    garmin, manual = _truncated_pair()
    for row in (garmin, manual):
        db.upsert_workout(row["activity_id"], row["day"], **{
            k: v for k, v in row.items() if k not in ("activity_id", "day")
        })
    before = db.recent_workouts(days=30, include_duplicates=True)
    preview = db.merge_workout_sources(source_activity_ids=[100, -100], dry_run=True)
    assert preview["merged"] is False
    assert preview["merges"][0]["would_merge"] is True
    assert preview["merges"][0]["duration_s"] == 3300
    assert db.recent_workouts(days=30, include_duplicates=True) == before


def test_repeated_merge_creates_no_extra_canonical(db: Database) -> None:
    garmin, manual = _truncated_pair()
    for row in (garmin, manual):
        db.upsert_workout(row["activity_id"], row["day"], **{
            k: v for k, v in row.items() if k not in ("activity_id", "day")
        })
    first = db.merge_workout_sources(source_activity_ids=[100, -100])
    canonical_id = first["merges"][0]["canonical_activity_id"]
    db.merge_workout_sources(day=garmin["day"])
    db.merge_workout_sources(day=garmin["day"])
    canonicals = [
        w for w in db.recent_workouts(days=30, include_duplicates=True)
        if w.get("source") == "merged"
    ]
    assert [c["activity_id"] for c in canonicals] == [canonical_id]
    assert len(db.recent_workouts(days=30)) == 1


def test_unmerge_restores_the_sources(db: Database) -> None:
    garmin, manual = _truncated_pair()
    for row in (garmin, manual):
        db.upsert_workout(row["activity_id"], row["day"], **{
            k: v for k, v in row.items() if k not in ("activity_id", "day")
        })
    merged = db.merge_workout_sources(source_activity_ids=[100, -100])
    canonical_id = merged["merges"][0]["canonical_activity_id"]
    db.unmerge_workout_sources(canonical_id)
    visible = {w["activity_id"] for w in db.recent_workouts(days=30)}
    assert {100, -100} <= visible


def test_progression_excludes_unsupported_records_and_reports_it(db: Database) -> None:
    from garmin_coach.activity_progress import build_activity_progress

    db.upsert_workout(
        400, _day(3), name="Good run", type="running", duration_s=1500,
        distance_m=5000, source="garmin", training_load=70,
    )
    db.upsert_workout(
        401, _day(2), name="Glitch", type="running", duration_s=600,
        distance_m=9000, source="garmin", training_load=20,
    )
    report = build_activity_progress(db, "running", days=30)
    assert 401 in report["data_quality"]["excluded_from_distance_and_pace"]
    assert report["summary"]["workout_count"] == 1
    assert report["records"]["best_session_pace"]["activity_id"] == 400
    assert any(f["type"] == "impossible_speed" for f in report["data_quality"]["findings"])
    assert "silently dropped" in report["data_quality"]["note"]


def test_empty_database_report_is_safe(db: Database) -> None:
    report = db.workout_data_quality(days=30)
    assert report["finding_count"] == 0
    assert report["findings"] == []
    assert report["records_blocked_activity_ids"] == []


def test_quality_report_helper_matches_the_database_service(db: Database) -> None:
    rows = _truncated_pair()
    direct = quality_report(rows, [], days=30)
    for row in rows:
        db.upsert_workout(row["activity_id"], row["day"], **{
            k: v for k, v in row.items() if k not in ("activity_id", "day")
        })
    via_db = db.workout_data_quality(days=30)
    assert direct["by_type"] == via_db["by_type"]
