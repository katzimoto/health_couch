"""Tests for longitudinal strength progression analytics (issue #8).

Offline and deterministic: synthetic strength sessions in a temp SQLite
database, plus direct exercise of the pure functions. No personal data.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_coach.database import Database
from garmin_coach.strength_progress import (
    E1RM_MAX_REPS,
    build_exercise_progress,
    build_strength_progress,
    detect_records,
    estimate_1rm,
    exercise_identity,
    frequency,
    load_convention,
    normalize_exercise_name,
    progression,
    session_performance,
)


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "strength.db"))


def _day(offset: int) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


# ── Alias normalization stays conservative ─────────────────────────────────────

def test_aliases_fold_case_spacing_and_unambiguous_abbreviations() -> None:
    assert normalize_exercise_name("Bench Press") == "bench press"
    assert normalize_exercise_name("  bench   press  ") == "bench press"
    assert normalize_exercise_name("DB Bench Press") == "dumbbell bench press"
    assert normalize_exercise_name("Pull-ups") == "pullup"
    assert normalize_exercise_name(None) is None
    assert normalize_exercise_name("") is None


def test_aliases_never_conflate_different_exercises_or_equipment() -> None:
    assert normalize_exercise_name("incline dumbbell press") != normalize_exercise_name(
        "dumbbell press"
    )
    assert normalize_exercise_name("leg press") != normalize_exercise_name("bench press")
    # Same movement, different machine → different identity.
    assert exercise_identity("leg press", "45° sled") != exercise_identity(
        "leg press", "horizontal machine"
    )


# ── Load conventions are explicit ──────────────────────────────────────────────

def test_load_conventions_are_named_not_assumed() -> None:
    assert load_convention("dumbbell bench press")["convention"] == "per_hand"
    assert load_convention("barbell squat")["convention"] == "total_load"
    assert load_convention("leg press", "45° sled")["convention"] == "machine_stack"
    assert load_convention("pull-up")["convention"] == "bodyweight"
    assert load_convention("assisted pull-up")["convention"] == "assisted"
    unknown = load_convention("mystery movement")
    assert unknown["convention"] == "unknown"
    assert "does not say" in unknown["note"]


# ── Estimated 1RM eligibility ──────────────────────────────────────────────────

def test_estimated_1rm_is_labelled_and_range_limited() -> None:
    good = estimate_1rm(100, 5)
    assert good["eligible"] is True
    assert good["value_kg"] == pytest.approx(116.7, abs=0.1)
    assert good["is_estimate"] is True
    assert "not a measured 1RM" in good["note"]

    too_many = estimate_1rm(60, E1RM_MAX_REPS + 5)
    assert too_many["eligible"] is False
    assert "outside the 1–10 rep range" in too_many["reason"]

    assert estimate_1rm(None, 5)["eligible"] is False
    assert estimate_1rm(100, None)["eligible"] is False


# ── Per-set volume is exact ────────────────────────────────────────────────────

def test_mixed_weight_volume_is_exact_from_the_recorded_sets() -> None:
    entry = {
        "date": _day(1), "session_id": 1, "exercise_name": "bench press",
        "actual_sets": [
            {"reps": 10, "weight_kg": 60},
            {"reps": 8, "weight_kg": 70},
            {"reps": 6, "weight_kg": 80},
        ],
        "weight_kg": 80, "sets": 3, "reps": 8, "best_set_weight_kg": 80,
    }
    perf = session_performance(entry)
    # 600 + 560 + 480 = 1640. The wrong answer (80 × 24) would be 1920.
    assert perf["completed_volume_kg"] == 1640.0
    assert perf["volume_basis"] == "per_set"
    assert perf["top_set_weight_kg"] == 80
    assert perf["working_weight_kg"] == 60
    assert perf["all_sets_at_top_weight"] is False


def test_aggregate_only_session_is_labelled_an_estimate() -> None:
    entry = {
        "date": _day(1), "session_id": 2, "sets": 3, "reps": 10, "weight_kg": 50,
        "best_set_weight_kg": 50, "estimated_volume_kg": 1500.0, "actual_sets": None,
    }
    perf = session_performance(entry)
    assert perf["volume_basis"] == "aggregate_estimate"
    assert perf["completed_volume_kg"] == 1500.0
    assert "not distinguishable" in perf["volume_note"]
    assert perf["working_weight_kg"] is None


def test_top_set_only_versus_all_sets_is_distinguishable() -> None:
    top_set_only = session_performance({
        "date": _day(2), "session_id": 3,
        "actual_sets": [
            {"reps": 8, "weight_kg": 70}, {"reps": 8, "weight_kg": 70},
            {"reps": 3, "weight_kg": 85},
        ],
    })
    all_sets = session_performance({
        "date": _day(1), "session_id": 4,
        "actual_sets": [
            {"reps": 6, "weight_kg": 85}, {"reps": 6, "weight_kg": 85},
            {"reps": 5, "weight_kg": 85},
        ],
    })
    assert top_set_only["top_set_weight_kg"] == all_sets["top_set_weight_kg"] == 85
    assert top_set_only["all_sets_at_top_weight"] is False
    assert all_sets["all_sets_at_top_weight"] is True

    records = detect_records([top_set_only, all_sets])
    assert records["heaviest_weight_kg"]["scope"] == "carried across every working set"


def test_skipped_and_substituted_work_never_inflates_volume() -> None:
    skipped = session_performance({
        "date": _day(1), "session_id": 5, "status": "skipped",
        "actual_sets": [{"reps": 10, "weight_kg": 60}],
    })
    assert skipped["counts_towards_volume"] is False
    assert skipped["completed_volume_kg"] is None
    assert skipped["usable_for_records"] is False

    substituted = session_performance({
        "date": _day(1), "session_id": 6, "status": "substituted",
        "substitute_exercise": "machine press",
        "actual_sets": [{"reps": 10, "weight_kg": 60}],
    })
    assert substituted["counts_towards_volume"] is False


def test_partial_sets_count_only_what_was_completed() -> None:
    perf = session_performance({
        "date": _day(1), "session_id": 7,
        "actual_sets": [
            {"reps": 10, "weight_kg": 60},
            {"reps": 4, "weight_kg": 60},   # cut short
            {"reps": None, "weight_kg": 60},  # never performed / unrecorded
        ],
    })
    assert perf["sets"] == 2  # the unusable set isn't counted as completed work
    assert perf["completed_volume_kg"] == 840.0


# ── Records ────────────────────────────────────────────────────────────────────

def test_unreadable_rows_are_excluded_from_records_not_guessed() -> None:
    clean = session_performance({
        "date": _day(2), "session_id": 8,
        "actual_sets": [{"reps": 5, "weight_kg": 80}],
    })
    dirty = session_performance({
        "date": _day(1), "session_id": 9, "weight_kg": 200,
        "best_set_weight_kg": 200,
        "data_quality": "unreadable stored values in: reps",
    })
    records = detect_records([clean, dirty])
    assert records["heaviest_weight_kg"]["value"] == 80  # not the dubious 200
    assert records["excluded_sessions"][0]["session_id"] == 9


def test_no_usable_sessions_claims_no_record() -> None:
    records = detect_records([])
    assert records["available"] is False
    assert "no record is claimed" in records["unavailable_reason"]


def test_reps_record_is_not_confused_with_a_weight_record() -> None:
    heavy = session_performance({
        "date": _day(2), "session_id": 10,
        "actual_sets": [{"reps": 3, "weight_kg": 100}],
    })
    light_many = session_performance({
        "date": _day(1), "session_id": 11,
        "actual_sets": [{"reps": 20, "weight_kg": 40}],
    })
    records = detect_records([heavy, light_many])
    assert records["heaviest_weight_kg"]["value"] == 100
    assert records["most_reps_in_a_set"]["value"] == 20
    assert records["most_reps_in_a_set"]["weight_kg"] == 40
    assert "not a heavier lift" in records["most_reps_in_a_set"]["note"]


def test_e1rm_record_only_from_eligible_sets() -> None:
    only_high_reps = session_performance({
        "date": _day(1), "session_id": 12,
        "actual_sets": [{"reps": 20, "weight_kg": 40}],
    })
    records = detect_records([only_high_reps])
    assert records["best_estimated_1rm"] is None
    assert "valid rep range" in records["e1rm_unavailable_reason"]


# ── Progression ────────────────────────────────────────────────────────────────

def test_progression_needs_two_sessions() -> None:
    single = [session_performance({
        "date": _day(1), "session_id": 13,
        "actual_sets": [{"reps": 5, "weight_kg": 80}],
    })]
    result = progression(single)
    assert result["available"] is False
    assert "single data point" in result["unavailable_reason"]
    assert result.get("rate") is None


def test_progression_reports_change_and_rate() -> None:
    perfs = [
        session_performance({
            "date": _day(28), "session_id": 14,
            "actual_sets": [{"reps": 8, "weight_kg": 60}] * 3,
        }),
        session_performance({
            "date": _day(14), "session_id": 15,
            "actual_sets": [{"reps": 8, "weight_kg": 65}] * 3,
        }),
        session_performance({
            "date": _day(0), "session_id": 16,
            "actual_sets": [{"reps": 8, "weight_kg": 70}] * 3,
        }),
    ]
    result = progression(perfs)
    assert result["available"] is True
    assert result["top_set_weight_kg"]["absolute_change"] == 10.0
    assert result["top_set_weight_kg"]["percent_change"] == pytest.approx(16.7, abs=0.1)
    assert result["rate"]["top_set_weight_kg_per_week"] == pytest.approx(2.5, abs=0.01)
    assert result["completed_volume_kg"]["absolute_change"] == pytest.approx(240.0)


def test_mixed_volume_bases_are_flagged() -> None:
    perfs = [
        session_performance({
            "date": _day(20), "session_id": 17, "sets": 3, "reps": 10,
            "weight_kg": 50, "best_set_weight_kg": 50, "estimated_volume_kg": 1500.0,
        }),
        session_performance({
            "date": _day(1), "session_id": 18,
            "actual_sets": [{"reps": 10, "weight_kg": 55}] * 3,
        }),
    ]
    result = progression(perfs)
    assert result["completed_volume_kg"]["mixed_basis"] is True
    assert "mixes two bases" in result["completed_volume_kg"]["caveat"]


def test_frequency_counts_only_performed_work() -> None:
    perfs = [
        session_performance({"date": _day(10), "session_id": 19,
                             "actual_sets": [{"reps": 5, "weight_kg": 80}]}),
        session_performance({"date": _day(3), "session_id": 20, "status": "skipped",
                             "actual_sets": [{"reps": 5, "weight_kg": 80}]}),
    ]
    freq = frequency(perfs, window_days=28)
    assert freq["sessions"] == 1
    assert freq["skipped_or_substituted"] == 1
    assert freq["sessions_per_week"] == pytest.approx(0.25, abs=0.01)


# ── End-to-end over the database ───────────────────────────────────────────────

def _log(db: Database, offset: int, exercises: list[dict], **session_fields) -> dict:
    return db.add_strength_session(
        _day(offset), exercises=exercises, duration_s=3600, **session_fields
    )


def test_end_to_end_single_exercise(db: Database) -> None:
    _log(db, 21, [{
        "exercise_name": "Bench Press",
        "actual_sets": [{"reps": 8, "weight_kg": 60, "rpe": 7}] * 3,
    }])
    _log(db, 7, [{
        "exercise_name": "bench press",
        "actual_sets": [
            {"reps": 8, "weight_kg": 65, "rpe": 7},
            {"reps": 8, "weight_kg": 65, "rpe": 8},
            {"reps": 6, "weight_kg": 70, "rpe": 9},
        ],
    }])
    report = build_exercise_progress(db, "bench press", days=60)

    assert report["available"] is True
    assert report["frequency"]["sessions"] == 2
    assert report["records"]["heaviest_weight_kg"]["value"] == 70
    assert report["records"]["heaviest_weight_kg"]["scope"] == "top set only"
    assert report["progression"]["top_set_weight_kg"]["absolute_change"] == 10.0
    # 8×65 + 8×65 + 6×70 = 1460, exact from the sets.
    latest = report["sessions"][-1]
    assert latest["completed_volume_kg"] == 1460.0
    assert latest["volume_basis"] == "per_set"
    assert latest["estimated_1rm"]["eligible"] is True


def test_end_to_end_reports_equipment_variants_separately(db: Database) -> None:
    _log(db, 10, [{
        "exercise_name": "leg press", "machine": "45° sled",
        "actual_sets": [{"reps": 10, "weight_kg": 150}],
    }])
    _log(db, 3, [{
        "exercise_name": "leg press", "machine": "horizontal machine",
        "actual_sets": [{"reps": 10, "weight_kg": 90}],
    }])
    report = build_exercise_progress(db, "leg press", days=60)
    assert report["equipment_variants"] == ["45° sled", "horizontal machine"]
    assert "not equivalent" in report["equipment_note"]
    assert report["load_convention"]["convention"] == "machine_stack"


def test_end_to_end_handles_legacy_string_values(db: Database) -> None:
    """Rows holding strings/rep ranges must degrade, never crash or guess."""
    _log(db, 5, [{
        "exercise_name": "squat", "sets": "3", "reps": "10-12", "weight_kg": "80",
        "planned_reps": "10-12",
    }])
    report = build_exercise_progress(db, "squat", days=60)
    assert report["available"] is True
    session = report["sessions"][0]
    assert session["top_set_weight_kg"] == 80.0
    # A rep range is a plan, never a rep count to multiply.
    assert session["avg_reps"] is None or session["completed_volume_kg"] is None


def test_end_to_end_no_history_is_explicit(db: Database) -> None:
    report = build_exercise_progress(db, "never done", days=60)
    assert report["available"] is False
    assert "no logged sessions" in report["unavailable_reason"]
    assert report["records"]["available"] is False
    assert report["progression"]["available"] is False


def test_all_exercise_summary_is_bounded(db: Database) -> None:
    for i in range(3):
        _log(db, i + 1, [
            {"exercise_name": f"exercise {i}",
             "actual_sets": [{"reps": 10, "weight_kg": 50 + i}]},
        ])
    summary = build_strength_progress(db, days=60, max_exercises=2)
    assert summary["exercise_count"] == 3
    assert len(summary["exercises"]) == 2
    assert summary["truncated"] is True
    assert all("load_convention" in e for e in summary["exercises"])


def test_empty_database_summary(db: Database) -> None:
    summary = build_strength_progress(db, days=60)
    assert summary["available"] is False
    assert "no strength exercises logged" in summary["unavailable_reason"]


def test_existing_exercise_history_and_recommendations_still_work(db: Database) -> None:
    """Backwards compatibility: the existing interfaces are untouched."""
    from garmin_coach.progression import recommend_next_weight

    _log(db, 4, [{
        "exercise_name": "row",
        "actual_sets": [{"reps": 10, "weight_kg": 50, "rpe": 6}] * 3,
    }])
    history = db.exercise_history("row", days=60)
    assert history and history[0]["weight_kg"] == 50
    recommendation = recommend_next_weight(history[0], exercise_name="row")
    assert recommendation["action"] == "increase"


def test_strength_session_merged_with_garmin_is_read_once(db: Database) -> None:
    """A manual log merged with a Garmin activity is still one session."""
    session = _log(db, 2, [{
        "exercise_name": "overhead press",
        "actual_sets": [{"reps": 8, "weight_kg": 40}] * 3,
    }])
    db.upsert_workout(
        9001, _day(2), name="Strength", type="strength_training",
        duration_s=3500, avg_hr=120, calories=300, training_load=80,
        source="garmin", load_source="garmin",
    )
    merged = db.merge_workout_sources(
        source_activity_ids=[session["activity_id"], 9001]
    )
    assert merged["merged"] is True
    report = build_exercise_progress(db, "overhead press", days=60)
    assert report["frequency"]["sessions"] == 1
    assert report["records"]["heaviest_weight_kg"]["value"] == 40
