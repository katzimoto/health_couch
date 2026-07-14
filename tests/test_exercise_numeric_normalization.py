"""Regression tests for the exercise numeric-type bug.

ChatGPT tool calls and legacy imports stored strings ("3", "12.5"), rep
ranges ("10-12"), and lists ([10, 10, 9]) in columns the calculations trusted
as numbers, so ``get_exercise_history`` and ``recommend_next_weights`` died
with ``can't multiply sequence by non-int of type 'float'``. All math now
goes through ``garmin_coach.exercise_metrics``; these tests pin the parsers,
the normalized model, graceful degradation of malformed rows, and the two
endpoints end to end.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import text

import garmin_coach.mcp_server as mcp
from garmin_coach.mcp_tools import runtime
from garmin_coach.database import Database
from garmin_coach.exercise_metrics import (
    normalize_performance,
    parse_float,
    parse_int,
    parse_rep_range,
    parse_reps,
)
from garmin_coach.progression import recommend_next_weight


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Database:
    database = Database(path=str(tmp_path / "normalization.db"))
    # db and analyzer both derive from the runtime db handle.
    monkeypatch.setattr(runtime, "_db", database)
    monkeypatch.setattr(runtime, "_garmin", None)
    return database


def _day(offset: int = 0) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


def _insert_raw(db: Database, day: str, **exercise_fields) -> int:
    """Insert an exercise row exactly as legacy data left it, bypassing both
    ``_prepare_exercise`` and SQLAlchemy's bind-time type coercion. SQLite is
    dynamically typed, so strings land in numeric columns just like the real
    malformed rows did."""
    with db.session() as s:
        s.execute(
            text("INSERT INTO strength_session (day, ts) VALUES (:day, :ts)"),
            {"day": day, "ts": datetime.now(timezone.utc).isoformat()},
        )
        session_id = s.execute(text("SELECT last_insert_rowid()")).scalar_one()
        columns = ", ".join(["session_id", *exercise_fields])
        placeholders = ", ".join([":session_id", *(f":{k}" for k in exercise_fields)])
        s.execute(
            text(f"INSERT INTO strength_exercise ({columns}) VALUES ({placeholders})"),
            {"session_id": session_id, **exercise_fields},
        )
        s.commit()
        return session_id


# ── Parsers ─────────────────────────────────────────────────────────────────────

def test_parse_float_accepts_numbers_and_numeric_strings() -> None:
    assert parse_float(12.5) == 12.5
    assert parse_float(3) == 3.0
    assert parse_float("12.5") == 12.5
    assert parse_float(" 80 ") == 80.0
    assert parse_float("3") == 3.0


def test_parse_float_rejects_junk_without_guessing() -> None:
    assert parse_float(None) is None
    assert parse_float("") is None
    assert parse_float("   ") is None
    assert parse_float("unknown") is None
    assert parse_float("10-12") is None
    assert parse_float(True) is None
    assert parse_float([10]) is None
    assert parse_float(float("nan")) is None
    assert parse_float(float("inf")) is None


def test_parse_int_accepts_integral_values_only() -> None:
    assert parse_int(3) == 3
    assert parse_int("3") == 3
    assert parse_int(10.0) == 10
    assert parse_int("10.0") == 10
    assert parse_int("10.5") is None  # never silently truncated
    assert parse_int("10-12") is None
    assert parse_int("") is None
    assert parse_int(None) is None


def test_parse_rep_range_bounds() -> None:
    assert parse_rep_range("10-12") == (10, 12)
    assert parse_rep_range("8 - 10") == (8, 10)
    assert parse_rep_range("8–10") == (8, 10)  # en dash
    assert parse_rep_range("8 to 10") == (8, 10)
    assert parse_rep_range("12-8") == (8, 12)  # reversed bounds normalized
    assert parse_rep_range(10) == (10, 10)
    assert parse_rep_range("10") == (10, 10)
    assert parse_rep_range(None) == (None, None)
    assert parse_rep_range("amrap") == (None, None)


def test_parse_reps_scalar_list_and_json() -> None:
    assert parse_reps(10) == [10]
    assert parse_reps("10") == [10]
    assert parse_reps([10, 10, 9]) == [10, 10, 9]
    assert parse_reps(["10", "10", "9"]) == [10, 10, 9]
    assert parse_reps("[10, 10, 9]") == [10, 10, 9]  # JSON-encoded legacy list
    assert parse_reps(["10", "x", 9]) == [10, 9]  # bad entries dropped
    assert parse_reps("10-12") == []  # a plan, not a count
    assert parse_reps(None) == []
    assert parse_reps("") == []


# ── Normalized performance model ────────────────────────────────────────────────

def test_normalize_numeric_aggregates() -> None:
    perf = normalize_performance({"sets": 3, "reps": 10, "weight_kg": 12.5, "rpe": 7})
    assert perf.sets == 3
    assert perf.reps == [10]
    assert perf.average_reps == 10
    assert perf.best_reps == 10
    assert perf.weight_kg == 12.5
    assert perf.volume == 375.0
    assert perf.rpe == 7.0


def test_normalize_string_aggregates() -> None:
    perf = normalize_performance({"sets": "3", "reps": "10", "weight_kg": "12.5"})
    assert perf.sets == 3
    assert perf.weight_kg == 12.5
    assert perf.volume == 375.0


def test_normalize_per_set_data_takes_precedence() -> None:
    perf = normalize_performance({
        "sets": 99, "reps": 1, "weight_kg": 1,  # bogus aggregates
        "actual_sets": [
            {"reps": 12, "weight_kg": 10, "rpe": 7},
            {"reps": 11, "weight_kg": 10, "rpe": 8},
            {"reps": 10, "weight_kg": 10, "rpe": 8},
        ],
    })
    assert perf.sets == 3
    assert perf.reps == [12, 11, 10]
    assert perf.best_reps == 12
    assert perf.weight_kg == 10.0
    assert perf.volume == 330.0


def test_normalize_mixed_string_per_set_data() -> None:
    perf = normalize_performance({
        "actual_sets": [
            {"reps": "12", "weight_kg": "10"},
            {"reps": "11", "weight_kg": "10"},
        ],
    })
    assert perf.volume == 230.0
    assert perf.best_set_weight_kg == 10.0


def test_normalize_missing_values_yield_none_not_crash() -> None:
    perf = normalize_performance({"reps": None, "weight_kg": None})
    assert perf.volume is None
    assert perf.weight_kg is None
    assert perf.reps == []


def test_normalize_invalid_values_yield_none(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="garmin_coach.exercise_metrics"):
        perf = normalize_performance(
            {"reps": "unknown", "weight_kg": "", "sets": 3},
            endpoint="get_exercise_history",
            exercise_name="Leg Press",
            session_id=7,
            row_id=42,
        )
    assert perf.volume is None
    assert perf.weight_kg is None
    assert perf.sets == 3  # the valid portion survives
    # Structured warning names the endpoint, row, field, and raw value.
    warning = "\n".join(r.message for r in caplog.records)
    assert "endpoint=get_exercise_history" in warning
    assert "exercise='Leg Press'" in warning
    assert "session_id=7" in warning
    assert "exercise_row_id=42" in warning
    assert "field=reps" in warning
    assert "raw='unknown'" in warning


def test_normalize_legacy_rep_list_treated_as_per_set_counts() -> None:
    perf = normalize_performance({"sets": 3, "reps": [10, 10, 9], "weight_kg": 40})
    assert perf.reps == [10, 10, 9]
    assert perf.average_reps == pytest.approx(9.67, abs=0.01)
    assert perf.volume == (10 + 10 + 9) * 40


def test_normalize_rep_range_never_multiplied() -> None:
    # A range stored where an actual count belongs is unusable for volume.
    perf = normalize_performance({"sets": 3, "reps": "10-12", "weight_kg": 12.5})
    assert perf.reps == []
    assert perf.volume is None
    assert perf.weight_kg == 12.5  # still returned


def test_normalize_partial_per_set_rows_skip_only_invalid_sets() -> None:
    perf = normalize_performance({
        "actual_sets": [
            {"reps": 10, "weight_kg": 80},
            {"reps": "many", "weight_kg": 80},  # unusable rep count
            "not-a-dict",
            {"reps": 9, "weight_kg": 80},
        ],
    })
    assert perf.volume == (10 + 9) * 80
    assert perf.reps == [10, 9]


# ── recommend_next_weight over malformed history entries ───────────────────────

def test_recommendation_handles_string_weight_and_rpe() -> None:
    rec = recommend_next_weight({"best_set_weight_kg": "100", "rpe": "9"})
    assert rec["action"] == "reduce"
    assert rec["recommended_weight_kg"] == 95.0

    rec = recommend_next_weight({"weight_kg": "12.5", "rpe": "6.5"})
    assert rec["action"] == "increase"
    assert rec["recommended_weight_kg"] == 15.0  # 12.5 + one plate step


def test_recommendation_never_increases_on_unreadable_data() -> None:
    unreadable_weight = recommend_next_weight({"weight_kg": "unknown"})
    assert unreadable_weight["action"] == "log_first"
    assert unreadable_weight["recommended_weight_kg"] is None
    assert unreadable_weight["data_quality"] is not None

    unreadable_rpe = recommend_next_weight({"weight_kg": 80, "rpe": "hard"})
    assert unreadable_rpe["action"] == "maintain"  # not increase
    assert unreadable_rpe["recommended_weight_kg"] == 80
    assert "rpe" in unreadable_rpe["data_quality"]


def test_recommendation_uses_rep_range_bounds_for_progression() -> None:
    # No RPE: only top of the planned range earns an increase.
    below_range_top = recommend_next_weight(
        {"weight_kg": 80, "reps": 10, "planned_reps": "10-12"}
    )
    assert below_range_top["action"] == "maintain"
    assert below_range_top["recommended_weight_kg"] == 80

    at_range_top = recommend_next_weight(
        {"weight_kg": 80, "reps": 12, "planned_reps": "10-12"}
    )
    assert at_range_top["action"] == "increase"


def test_recommendation_no_weight_at_all_still_log_first() -> None:
    rec = recommend_next_weight({"weight_kg": None})
    assert rec["action"] == "log_first"
    assert rec["data_quality"] is None


# ── Write-path hardening ────────────────────────────────────────────────────────

def test_logging_string_values_no_longer_crashes_and_is_normalized(db: Database) -> None:
    session = db.add_strength_session(
        _day(),
        exercises=[{
            "exercise_name": "Leg Press",
            "actual_sets": [
                {"reps": "12", "weight_kg": "80"},
                {"reps": "11", "weight_kg": "80", "rpe": "7.5"},
            ],
        }],
    )
    ex = session["exercises"][0]
    assert ex["sets"] == 2
    assert ex["reps"] == 12  # round(mean(12, 11))
    assert ex["weight_kg"] == 80.0
    assert ex["rpe"] == 7.5


def test_logging_list_reps_is_stored_recoverably(db: Database) -> None:
    db.add_strength_session(
        _day(),
        exercises=[{"exercise_name": "Lat Pulldown", "sets": "3",
                    "reps": [10, 10, 9], "weight_kg": "40"}],
    )
    entry = db.exercise_history("Lat Pulldown")[0]
    assert entry["sets"] == 3
    assert entry["reps"] == 10  # round(mean(10, 10, 9))
    assert entry["estimated_volume_kg"] == (10 + 10 + 9) * 40


# ── Endpoint regressions (the reported failures) ────────────────────────────────

_REPORTED_EXERCISES = [
    "Leg Press", "Dumbbell Bench Press", "Lat Pulldown", "Romanian Deadlift",
    "Seated Cable Row", "Dumbbell Shoulder Press", "Plank",
]


def _seed_malformed_history(db: Database) -> None:
    """Rows shaped like the production data that broke the endpoints."""
    _insert_raw(db, _day(10), exercise_name="Dumbbell Bench Press",
                sets="3", reps="10-12", weight_kg="12.5", rpe="7")
    _insert_raw(db, _day(9), exercise_name="Leg Press",
                sets=3, reps="[10, 10, 9]", weight_kg="80")
    _insert_raw(
        db, _day(8), exercise_name="Lat Pulldown", sets="3",
        set_details='[{"reps": "12", "weight_kg": "40"}, {"reps": "11", "weight_kg": "40"}]',
    )
    _insert_raw(db, _day(7), exercise_name="Seated Cable Row",
                sets="3", reps="unknown", weight_kg="")
    _insert_raw(db, _day(6), exercise_name="Romanian Deadlift",
                sets=3, reps=8, weight_kg=60, rpe=7)  # a clean row among the junk
    _insert_raw(db, _day(5), exercise_name="Dumbbell Shoulder Press",
                sets="4", reps="8", weight_kg="15", planned_reps="8-10")
    _insert_raw(db, _day(4), exercise_name="Plank", notes="3x60s hold")


def test_get_exercise_history_survives_malformed_rows(db: Database) -> None:
    _seed_malformed_history(db)

    bench = mcp.get_exercise_history(
        exercise_name="Dumbbell Bench Press", days=120, limit=10
    )
    assert len(bench) == 1
    assert bench[0]["weight_kg"] == 12.5
    assert bench[0]["rpe"] == 7.0
    assert bench[0]["reps"] is None  # "10-12" is not a rep count
    assert bench[0]["estimated_volume_kg"] is None  # null, not a crash

    leg_press = mcp.get_exercise_history(exercise_name="Leg Press", days=120, limit=10)
    assert leg_press[0]["estimated_volume_kg"] == (10 + 10 + 9) * 80
    assert leg_press[0]["reps"] == 10

    pulldown = mcp.get_exercise_history(exercise_name="Lat Pulldown", days=120, limit=10)
    assert pulldown[0]["estimated_volume_kg"] == (12 + 11) * 40
    assert pulldown[0]["best_set_weight_kg"] == 40.0

    row = mcp.get_exercise_history(exercise_name="Seated Cable Row", days=120, limit=10)
    assert row[0]["reps"] is None
    assert row[0]["weight_kg"] is None
    assert row[0]["estimated_volume_kg"] is None
    assert row[0]["sets"] == 3  # valid portion of the record kept
    assert "reps" in row[0]["data_quality"]
    assert "weight_kg" in row[0]["data_quality"]


def test_recommend_next_weights_survives_malformed_rows(db: Database) -> None:
    _seed_malformed_history(db)

    result = mcp.recommend_next_weights(exercises=_REPORTED_EXERCISES, days=120)
    by_name = {r["exercise"]: r for r in result["recommendations"]}
    assert set(by_name) == set(_REPORTED_EXERCISES)

    # Clean row → normal progression.
    assert by_name["Romanian Deadlift"]["action"] == "increase"
    # String numerics parse and recommend normally (RPE 7 → increase zone).
    assert by_name["Dumbbell Bench Press"]["action"] in ("increase", "maintain")
    assert by_name["Dumbbell Bench Press"]["recommended_weight_kg"] is not None
    # Range-planned, reps below the top of 8-10 → no increase without RPE.
    assert by_name["Dumbbell Shoulder Press"]["action"] == "maintain"
    # Unreadable reps/weight must not produce an increase, and the
    # degradation is surfaced rather than mistaken for "never logged".
    assert by_name["Seated Cable Row"]["action"] != "increase"
    assert "weight_kg" in by_name["Seated Cable Row"]["data_quality"]
    assert "couldn't be read" in by_name["Seated Cable Row"]["reason"]
    # Never-weighted exercise stays log_first.
    assert by_name["Plank"]["action"] == "log_first"


def test_full_reported_payloads_no_longer_error(db: Database) -> None:
    _seed_malformed_history(db)
    for name in ("Dumbbell Bench Press", "Leg Press", "Lat Pulldown"):
        mcp.get_exercise_history(exercise_name=name, days=120, limit=10)
    mcp.recommend_next_weights(exercises=_REPORTED_EXERCISES, days=120)
    mcp.recommend_next_weights(days=120)  # unspecified → recently trained
