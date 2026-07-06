"""Field-level merge of a manual strength log with a Garmin activity.

Row-level dedupe keeps one source and hides the rest; strength needs a hybrid:
manual is canonical for exercise details, Garmin for physiology/load. These
tests cover the pure matcher/field-priority functions, the DB merge service,
the MCP tools, and the exact real-world acceptance scenario from the brief.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import garmin_coach.mcp_server as mcp
from garmin_coach.database import Database
from garmin_coach.models import WorkoutSourceLink
from garmin_coach.workout_merge import (
    best_strength_match,
    merge_fields,
    strength_match,
)
from sqlmodel import select


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Database:
    database = Database(path=str(tmp_path / "merge.db"))
    monkeypatch.setattr(mcp, "db", database)
    return database


def _day(offset: int = 0) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


# The exact acceptance-criteria session.
_ACCEPTANCE_EXERCISES = [
    {"exercise_name": "Dumbbell bench press", "weight_kg": 12, "sets": 2, "reps": 12, "rpe": 6},
    {"exercise_name": "Dumbbell shoulder press", "weight_kg": 7, "sets": 2, "reps": 10, "rpe": 6},
    {"exercise_name": "Leg press", "weight_kg": 70, "sets": 2, "reps": 12},
    {"exercise_name": "Lat pulldown", "weight_kg": 36.5, "sets": 2, "reps": 12},
    {"exercise_name": "Seated cable row", "weight_kg": 35, "sets": 2, "reps": 12},
    {"exercise_name": "Plank", "notes": "45/45/45 sec"},
]


def _links(db: Database, canonical_id: int) -> list[WorkoutSourceLink]:
    with db.session() as s:
        return list(
            s.exec(
                select(WorkoutSourceLink).where(
                    WorkoutSourceLink.canonical_activity_id == canonical_id
                )
            ).all()
        )


def _all_links(db: Database) -> list[WorkoutSourceLink]:
    with db.session() as s:
        return list(s.exec(select(WorkoutSourceLink)).all())


def _seed_acceptance(db: Database, day: str | None = None) -> tuple[dict, int]:
    """Manual strength log + same-day Garmin strength activity, unmerged."""
    day = day or _day()
    session = db.add_strength_session(
        day,
        exercises=_ACCEPTANCE_EXERCISES,
        session_name="Easy recovery full-body strength",
        gym="Sports Center",
        notes="recovery-controlled session, no PR/no failure",
    )
    # Garmin's typeKey stays English ("strength_training") even when the UI
    # label is Hebrew ("כוח") — match reality: English type, Hebrew name.
    garmin_id = 900_001
    db.upsert_workout(
        garmin_id, day, name="כוח", type="strength_training",
        duration_s=3300, calories=260, avg_hr=118, max_hr=150,
        training_load=46.0, source="garmin", load_source="garmin",
        start_time=f"{day} 18:05:00",
    )
    return session, garmin_id


# ── Pure field-priority resolver ────────────────────────────────────────────────

def test_field_priority_manual_details_garmin_physiology() -> None:
    manual = {
        "source": "manual", "activity_id": -1, "type": "strength_training",
        "name": "Easy recovery full-body strength", "duration_s": 3000,
        "avg_hr": 108, "calories": 200, "training_load": 31.0, "load_source": "estimated",
    }
    garmin = {
        "source": "garmin", "activity_id": 5, "type": "strength_training",
        "name": "כוח", "duration_s": 3300, "avg_hr": 118, "max_hr": 150,
        "calories": 260, "training_load": 46.0, "load_source": "garmin",
        "start_time": "2026-07-05 18:05:00",
    }
    merged, provenance = merge_fields({"manual": manual, "garmin": garmin}, is_strength=True)

    # Physiology + load + duration + calories → Garmin.
    assert provenance["avg_hr"] == "garmin" and merged["avg_hr"] == 118
    assert provenance["max_hr"] == "garmin" and merged["max_hr"] == 150
    assert provenance["calories"] == "garmin" and merged["calories"] == 260
    assert provenance["duration_s"] == "garmin" and merged["duration_s"] == 3300
    assert provenance["training_load"] == "garmin" and merged["training_load"] == 46.0
    assert merged["load_source"] == "garmin"  # real Garmin load, not the estimate
    # Exercise details + name → manual (strength session).
    assert provenance["exercise_details"] == "manual"
    assert provenance["name"] == "manual"
    assert merged["name"] == "Easy recovery full-body strength"


def test_field_priority_falls_back_to_manual_load_without_garmin_load() -> None:
    manual = {"source": "manual", "activity_id": -1, "type": "strength_training",
              "training_load": 31.0, "load_source": "estimated"}
    garmin = {"source": "garmin", "activity_id": 5, "type": "strength_training",
              "training_load": None, "avg_hr": 120}
    merged, provenance = merge_fields({"manual": manual, "garmin": garmin}, is_strength=True)
    assert provenance["training_load"] == "manual"
    assert merged["training_load"] == 31.0
    assert merged["load_source"] == "estimated"  # no Garmin load → estimate stands


def test_field_priority_cardio_name_prefers_garmin() -> None:
    manual = {"source": "manual", "activity_id": -1, "type": "running", "name": "My run"}
    garmin = {"source": "garmin", "activity_id": 5, "type": "running",
              "name": "Morning Run", "avg_hr": 150}
    merged, provenance = merge_fields({"manual": manual, "garmin": garmin}, is_strength=False)
    assert provenance["name"] == "garmin" and merged["name"] == "Morning Run"
    assert "exercise_details" not in provenance  # cardio: no exercise log


# ── Pure matcher ────────────────────────────────────────────────────────────────

def test_match_by_date_only_when_manual_has_no_time() -> None:
    manual = {"type": "strength_training", "duration_s": 3000}
    garmin = {"type": "strength_training", "duration_s": 3300,
              "start_time": "2026-07-05 18:05:00"}
    result = strength_match(manual, garmin)
    assert result is not None
    confidence, reason = result
    assert 0.5 <= confidence <= 1.0
    assert "date only" in reason


def test_match_confidence_rises_with_close_timing() -> None:
    manual = {"type": "strength_training", "start_time": "2026-07-05 18:00:00", "duration_s": 3300}
    garmin = {"type": "strength_training", "start_time": "2026-07-05 18:20:00", "duration_s": 3300}
    close = strength_match(manual, garmin)
    far_manual = {"type": "strength_training", "duration_s": 3300}  # no time
    date_only = strength_match(far_manual, garmin)
    assert close is not None and date_only is not None
    assert close[0] > date_only[0]


def test_match_rejects_incompatible() -> None:
    strength = {"type": "strength_training", "start_time": "2026-07-05 07:00:00"}
    # Different activity type.
    assert strength_match(strength, {"type": "running", "start_time": "2026-07-05 07:00:00"}) is None
    # Too far apart in time.
    assert strength_match(
        {"type": "strength_training", "start_time": "2026-07-05 07:00:00"},
        {"type": "strength_training", "start_time": "2026-07-05 18:00:00"},
    ) is None
    # Durations too different.
    assert strength_match(
        {"type": "strength_training", "duration_s": 600},
        {"type": "strength_training", "duration_s": 3600},
    ) is None


def test_best_match_picks_highest_confidence() -> None:
    manual = {"type": "strength_training", "start_time": "2026-07-05 18:00:00", "duration_s": 3300}
    near = {"activity_id": 2, "type": "strength_training",
            "start_time": "2026-07-05 18:10:00", "duration_s": 3300}
    far = {"activity_id": 3, "type": "strength_training",
           "start_time": "2026-07-05 20:30:00", "duration_s": 3300}
    match = best_strength_match(manual, [far, near])
    assert match is not None
    chosen, _confidence, _reason = match
    assert chosen["activity_id"] == 2


# ── Acceptance scenario (DB + tools) ────────────────────────────────────────────

def test_acceptance_manual_plus_garmin_merge_into_one_workout(db: Database) -> None:
    session, garmin_id = _seed_acceptance(db)

    assert db.latest_summary()["workout_count"] == 2  # two rows before merge

    result = mcp.merge_workout_sources(day=_day())
    assert result["merged"] is True
    merge = result["merges"][0]
    canonical_id = merge["canonical_activity_id"]

    # One workout counted, once, in summary and training load.
    summary = db.latest_summary()
    assert summary["workout_count"] == 1
    assert summary["training_load"] == 46.0  # Garmin load, counted once

    merged = db.get_merged_workout(canonical_id)
    assert merged["is_merged"] is True
    fs = merged["field_sources"]
    assert fs["exercise_details"] == "manual"       # exercise details source
    assert fs["avg_hr"] == "garmin"                 # HR source
    assert fs["calories"] == "garmin"               # calories source
    assert fs["duration_s"] == "garmin"             # duration source
    assert fs["training_load"] == "garmin"          # training load source
    # Garmin physiology attached.
    assert merged["physiology"]["avg_hr"] == 118
    assert merged["physiology"]["calories"] == 260
    assert merged["physiology"]["training_load"] == 46.0
    assert merged["physiology"]["load_source"] == "garmin"

    # Manual exercise details preserved exactly — Garmin never overwrites order.
    exercises = merged["strength_sessions"][0]["exercises"]
    names = [e["exercise_name"] for e in exercises]
    assert names == [e["exercise_name"] for e in _ACCEPTANCE_EXERCISES]
    bench = exercises[0]
    assert bench["weight_kg"] == 12 and bench["reps"] == 12 and bench["rpe"] == 6

    # Both source rows linked and preserved (never deleted).
    linked_ids = {ls["activity_id"] for ls in merged["linked_sources"]}
    assert garmin_id in linked_ids
    assert session["activity_id"] in linked_ids
    # The strength session now points at the canonical (not a hidden duplicate).
    assert db.get_strength_session(session["id"])["activity_id"] == canonical_id


def test_training_load_uses_garmin_and_counts_merged_once(db: Database) -> None:
    _seed_acceptance(db)
    mcp.merge_workout_sources(day=_day())

    load = mcp.get_training_load(days=28)
    non_dup = [w for w in load["recent_workouts"]]
    assert len(non_dup) == 1  # the merged canonical only
    canonical = non_dup[0]
    assert canonical["source"] == "merged"
    assert canonical["training_load"] == 46.0
    assert canonical["load_source"] == "garmin"

    assert len(load["merged_workouts"]) == 1
    label = load["merged_workouts"][0]["label"]
    assert "manual exercise log" in label and "Garmin" in label


def test_no_garmin_fallback_keeps_manual_estimated_load(db: Database) -> None:
    """With no Garmin activity, the manual session still works with an
    estimated load and merging is simply a no-op."""
    session = db.add_strength_session(
        _day(), exercises=_ACCEPTANCE_EXERCISES,
        session_name="Easy recovery full-body strength", duration_s=3000,
    )
    result = mcp.merge_workout_sources(day=_day())
    assert result["merged"] is False

    workouts = db.recent_workouts(days=7)
    assert len(workouts) == 1
    assert workouts[0]["source"] == "manual"
    assert workouts[0]["training_load"] is not None
    assert workouts[0]["load_source"] == "estimated"
    assert workouts[0]["activity_id"] == session["activity_id"]


def test_repeated_garmin_sync_does_not_create_duplicate_links(db: Database) -> None:
    _seed_acceptance(db)
    first = mcp.merge_workout_sources(day=_day())
    canonical_id = first["merges"][0]["canonical_activity_id"]

    # Garmin re-syncs the same activity (field-preserving upsert) and we merge
    # again — the classic "daily pull runs, merge runs" loop.
    db.upsert_workout(
        900_001, _day(), name="כוח", type="strength_training",
        duration_s=3300, calories=265, avg_hr=119, max_hr=151,
        training_load=46.0, source="garmin", load_source="garmin",
        start_time=f"{_day()} 18:05:00",
    )
    second = mcp.merge_workout_sources(day=_day())
    assert second["merges"][0]["canonical_activity_id"] == canonical_id

    links = _all_links(db)
    assert len(links) == 2  # one manual, one garmin — not duplicated
    assert {l.source for l in links} == {"manual", "garmin"}
    assert db.latest_summary()["workout_count"] == 1  # still counted once


def test_manual_details_survive_garmin_sync_and_dedupe(db: Database) -> None:
    session, _garmin_id = _seed_acceptance(db)
    original_names = [e["exercise_name"] for e in db.get_strength_session(session["id"])["exercises"]]

    # dedupe_workouts now performs the field-level merge as part of its work.
    result = db.dedupe_workouts(days=30)
    assert len(result["merged_sessions"]) == 1

    after = db.get_strength_session(session["id"])
    assert [e["exercise_name"] for e in after["exercises"]] == original_names
    # And a subsequent Garmin re-sync + dedupe still doesn't touch details.
    db.upsert_workout(
        900_001, _day(), type="strength_training", avg_hr=121,
        source="garmin", load_source="garmin",
    )
    db.dedupe_workouts(days=30)
    again = db.get_strength_session(session["id"])
    assert [e["exercise_name"] for e in again["exercises"]] == original_names


def test_source_priority_change_does_not_destroy_manual_details(db: Database) -> None:
    session, _garmin_id = _seed_acceptance(db)
    canonical_id = mcp.merge_workout_sources(day=_day())["merges"][0]["canonical_activity_id"]

    # Flip the row-level priority and re-dedupe — the field-level merge is
    # domain-based, so exercise details and Garmin physiology both survive.
    mcp.set_activity_source_priority(["manual", "garmin", "apple"])
    db.dedupe_workouts(days=30)

    merged = db.get_merged_workout(canonical_id)
    assert merged["field_sources"]["exercise_details"] == "manual"
    assert merged["physiology"]["avg_hr"] == 118  # Garmin physiology intact
    names = [e["exercise_name"] for e in merged["strength_sessions"][0]["exercises"]]
    assert names == [e["exercise_name"] for e in _ACCEPTANCE_EXERCISES]


def test_editing_merged_session_keeps_garmin_physiology(db: Database) -> None:
    """Editing exercises must not clobber the Garmin HR/load on the canonical."""
    session, _garmin_id = _seed_acceptance(db)
    canonical_id = mcp.merge_workout_sources(day=_day())["merges"][0]["canonical_activity_id"]

    db.update_strength_session(
        session["id"],
        exercises=_ACCEPTANCE_EXERCISES + [
            {"exercise_name": "Face pull", "weight_kg": 20, "sets": 2, "reps": 15},
        ],
    )
    merged = db.get_merged_workout(canonical_id)
    # Physiology + real load still Garmin's, not the re-estimated manual load.
    assert merged["physiology"]["avg_hr"] == 118
    assert merged["physiology"]["training_load"] == 46.0
    assert merged["field_sources"]["training_load"] == "garmin"
    # New exercise landed on the manual details.
    names = [e["exercise_name"] for e in merged["strength_sessions"][0]["exercises"]]
    assert "Face pull" in names
    assert db.latest_summary()["workout_count"] == 1  # still one session


def test_unmerge_restores_both_sources(db: Database) -> None:
    session, garmin_id = _seed_acceptance(db)
    canonical_id = mcp.merge_workout_sources(day=_day())["merges"][0]["canonical_activity_id"]
    assert db.latest_summary()["workout_count"] == 1

    result = mcp.unmerge_workout_sources(canonical_id)
    assert result["unmerged"] is True

    # Both source rows are back and count independently; the canonical is gone.
    assert db.get_merged_workout(canonical_id) is None
    ids = {w["activity_id"] for w in db.recent_workouts(days=7)}
    assert garmin_id in ids and session["activity_id"] in ids
    assert db.latest_summary()["workout_count"] == 2
    # Strength session reattached to its own manual row; details intact.
    reattached = db.get_strength_session(session["id"])
    assert reattached["activity_id"] == session["activity_id"]
    assert len(reattached["exercises"]) == len(_ACCEPTANCE_EXERCISES)
    assert _all_links(db) == []


def test_manual_relink_specific_rows_overrides_a_bad_match(db: Database) -> None:
    session, garmin_id = _seed_acceptance(db)
    result = mcp.merge_workout_sources(
        source_activity_ids=[session["activity_id"], garmin_id]
    )
    assert result["merged"] is True
    merge = result["merges"][0]
    assert merge["match_confidence"] == 1.0
    assert merge["match_reason"] == "manual link"
    assert db.latest_summary()["workout_count"] == 1


def test_low_confidence_match_skipped_unless_forced(db: Database) -> None:
    # Manual and Garmin strength far apart in time but same day: the timed
    # match is rejected outright, so nothing merges without force.
    db.add_strength_session(
        _day(), exercises=[{"exercise_name": "Squat", "weight_kg": 60}],
        session_name="AM lift", duration_s=3000,
    )
    # Give the manual row a start time so a rejecting time-gap can apply.
    manual = [w for w in db.recent_workouts(days=2) if w["source"] == "manual"][0]
    db.update_workout(manual["activity_id"], start_time=f"{_day()} 07:00:00")
    db.upsert_workout(
        900_050, _day(), name="כוח", type="strength_training", duration_s=3200,
        avg_hr=120, source="garmin", load_source="garmin",
        start_time=f"{_day()} 20:00:00",  # 13h apart → rejected
    )
    result = mcp.merge_workout_sources(day=_day())
    assert result["merged"] is False
    assert db.latest_summary()["workout_count"] == 2


def test_dry_run_previews_without_changing_data(db: Database) -> None:
    _seed_acceptance(db)
    result = mcp.merge_workout_sources(day=_day(), dry_run=True)
    assert result["dry_run"] is True
    assert result["merges"][0]["would_merge"] is True
    assert db.latest_summary()["workout_count"] == 2  # untouched
    assert _all_links(db) == []
