"""Tests for the five coaching upgrades: update_training_plan,
create_workout_reminder_pack, get_nutrition_gaps,
merge_garmin_strength_fragments, and the Telegram workout-log flow."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import garmin_coach.mcp_server as mcp
from garmin_coach.database import Database
from garmin_coach.reminders import Reminders
from garmin_coach.workout_flow import WorkoutLogFlows


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Database:
    database = Database(path=str(tmp_path / "top5.db"))
    monkeypatch.setattr(mcp, "db", database)
    monkeypatch.setattr(mcp, "reminders", Reminders(database))
    monkeypatch.setattr(mcp, "workout_log_flows", WorkoutLogFlows(database))
    return database


def _day(offset: int = 0) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


# ── Feature 3: update_training_plan ─────────────────────────────────────────────

def test_update_training_plan_partial_preserves_other_fields(db: Database) -> None:
    plan = db.create_training_plan(
        _day(), title="Push day", goal="hypertrophy", planned_start_time="18:00",
        estimated_duration_s=3600, intensity_target="RPE 7",
    )
    updated = mcp.update_training_plan(plan["id"], intensity_target="RPE 6")
    assert updated["intensity_target"] == "RPE 6"
    assert updated["title"] == "Push day"  # untouched
    assert updated["planned_start_time"] == "18:00"  # untouched
    assert updated["updated_at"] is not None


def test_update_training_plan_replaces_exercises_as_json(db: Database) -> None:
    plan = db.create_training_plan(
        _day(), title="Legs", exercises=[{"exercise_name": "Squat", "sets": 3}]
    )
    updated = mcp.update_training_plan(
        plan["id"], exercises=[{"exercise_name": "Leg press", "sets": 4}]
    )
    assert updated["exercises"] == [{"exercise_name": "Leg press", "sets": 4}]


def test_update_training_plan_rejects_unknown_status(db: Database) -> None:
    plan = db.create_training_plan(_day(), title="Run")
    result = mcp.update_training_plan(plan["id"], status="cancelled")
    assert "error" in result
    # Unaffected — bad status must not have been applied.
    assert db.get_training_plan(plan["id"])["status"] == "planned"


def test_update_training_plan_missing_id_returns_error(db: Database) -> None:
    assert "error" in mcp.update_training_plan(999_999, title="x")


def test_update_training_plan_records_history(db: Database) -> None:
    plan = db.create_training_plan(_day(), title="Push day", planned_start_time="18:00")
    mcp.update_training_plan(plan["id"], planned_start_time="20:00")
    mcp.update_training_plan(plan["id"], status="skipped", notes="sore")
    history = db.training_plan_history(plan["id"])
    assert len(history) == 2
    assert history[0]["changes"]["planned_start_time"] == {"old": "18:00", "new": "20:00"}
    assert history[1]["changes"]["status"] == {"old": "planned", "new": "skipped"}


def test_update_training_plan_no_op_when_nothing_changes(db: Database) -> None:
    plan = db.create_training_plan(_day(), title="Push day")
    mcp.update_training_plan(plan["id"], title="Push day")  # same value
    assert db.training_plan_history(plan["id"]) == []


# ── Feature 1: create_workout_reminder_pack ─────────────────────────────────────

def test_reminder_pack_computes_all_five_times_from_plan(db: Database) -> None:
    plan = db.create_training_plan(
        _day(), title="Leg day", planned_start_time="20:00",
    )
    result = mcp.create_workout_reminder_pack(plan["id"])
    assert result["plan_id"] == plan["id"]
    assert result["workout_time"] == "20:00"
    by_type = {r["type"]: r for r in result["reminders"]}
    assert by_type["pre_workout_meal"]["time"] == "18:00"
    assert by_type["hydration"]["time"] == "19:15"
    assert by_type["gym_start"]["time"] == "20:00"
    assert by_type["post_workout_meal"]["time"] == "21:45"  # 60 min default + 45
    assert by_type["workout_log"]["time"] == "22:00"
    assert all(r["deduplicated"] is False for r in result["reminders"])


def test_reminder_pack_uses_estimated_duration(db: Database) -> None:
    plan = db.create_training_plan(
        _day(), title="Long run", planned_start_time="07:00",
        estimated_duration_s=5400,  # 90 min → ends 08:30
    )
    result = mcp.create_workout_reminder_pack(plan["id"])
    by_type = {r["type"]: r for r in result["reminders"]}
    assert by_type["post_workout_meal"]["time"] == "09:15"
    assert by_type["workout_log"]["time"] == "09:30"


def test_reminder_pack_is_idempotent(db: Database) -> None:
    plan = db.create_training_plan(_day(), title="Push day", planned_start_time="20:00")
    first = mcp.create_workout_reminder_pack(plan["id"])
    second = mcp.create_workout_reminder_pack(plan["id"])
    first_ids = sorted(r["reminder_id"] for r in first["reminders"])
    second_ids = sorted(r["reminder_id"] for r in second["reminders"])
    assert first_ids == second_ids
    assert all(r["deduplicated"] is True for r in second["reminders"])


def test_reminder_pack_does_not_collide_across_different_dated_plans(db: Database) -> None:
    """Two different plans with the same title/time on different days must
    each get their own reminders — Reminders.create()'s own dedup ignores
    date, so this only works if the pack bakes the date into the message."""
    plan_a = db.create_training_plan(_day(10), title="Push day", planned_start_time="18:00")
    plan_b = db.create_training_plan(_day(3), title="Push day", planned_start_time="18:00")

    result_a = mcp.create_workout_reminder_pack(plan_a["id"])
    result_b = mcp.create_workout_reminder_pack(plan_b["id"])

    ids_a = {r["type"]: r["reminder_id"] for r in result_a["reminders"]}
    ids_b = {r["type"]: r["reminder_id"] for r in result_b["reminders"]}
    assert set(ids_a.values()).isdisjoint(ids_b.values())
    assert all(r["deduplicated"] is False for r in result_b["reminders"])

    gym_start_b = mcp.reminders.get(ids_b["gym_start"])
    assert gym_start_b["date"] == _day(3)  # not silently left on plan_a's date


def test_reminder_pack_resyncs_when_plan_time_changes(db: Database) -> None:
    plan = db.create_training_plan(_day(), title="Push day", planned_start_time="18:00")
    first = mcp.create_workout_reminder_pack(plan["id"])
    gym_start_id = next(r["reminder_id"] for r in first["reminders"] if r["type"] == "gym_start")

    mcp.update_training_plan(plan["id"], planned_start_time="20:00")
    second = mcp.create_workout_reminder_pack(plan["id"])
    updated = next(r for r in second["reminders"] if r["type"] == "gym_start")
    assert updated["reminder_id"] == gym_start_id  # same row, resynced
    assert updated["time"] == "20:00"
    assert updated["deduplicated"] is True


def test_reminder_pack_missing_workout_time_errors_clearly(db: Database) -> None:
    plan = db.create_training_plan(_day(), title="Push day")  # no planned_start_time
    result = mcp.create_workout_reminder_pack(plan["id"])
    assert "error" in result
    assert "workout_time" in result["error"]


def test_reminder_pack_unknown_plan_errors(db: Database) -> None:
    assert "error" in mcp.create_workout_reminder_pack(999_999)


def test_reminder_pack_respects_include_flags(db: Database) -> None:
    plan = db.create_training_plan(_day(), title="Push day", planned_start_time="18:00")
    result = mcp.create_workout_reminder_pack(
        plan["id"], include_hydration=False, include_workout_log=False
    )
    types = {r["type"] for r in result["reminders"]}
    assert types == {"pre_workout_meal", "gym_start", "post_workout_meal"}


# ── Feature 4: get_nutrition_gaps ───────────────────────────────────────────────

def test_nutrition_gaps_reports_remaining_macros(db: Database) -> None:
    db.set_profile(
        calorie_target=2400, protein_target_g=150, carbs_target_g=290,
        fat_target_g=70, fiber_target_g=30,
    )
    db.add_meal("toast", day=_day(), calories=300, protein_g=10, carbs_g=40)
    db.add_meal("chicken bowl", day=_day(), calories=800, protein_g=55,
                carbs_g=90, fat_g=35, fiber_g=8, sugar_g=10)

    gaps = mcp.get_nutrition_gaps(_day())
    assert gaps["day"] == _day()
    assert gaps["targets"]["calories"] == 2400
    assert gaps["consumed"]["calories"] == 1100
    assert gaps["remaining"]["calories"] == 1300
    assert gaps["remaining"]["protein_g"] == 85.0
    assert gaps["remaining"]["carbs_g"] == 160.0
    assert gaps["remaining"]["fiber_g"] == 22.0
    assert gaps["sugar_status"] == "low"
    assert "Protein is behind target." in gaps["alerts"]
    assert gaps["recommended_next_meal"]["style"] == "high protein + carbs + vegetables"


def test_nutrition_gaps_missing_targets_are_null_not_zero(db: Database) -> None:
    db.add_meal("toast", day=_day(), calories=300)
    gaps = mcp.get_nutrition_gaps(_day())
    assert gaps["targets"]["calories"] is None
    assert gaps["remaining"]["calories"] is None
    assert gaps["sugar_status"] == "unknown"  # no sugar logged, not "low"


def test_nutrition_gaps_past_day_flags_all_unlogged_meals(db: Database) -> None:
    day = _day(5)
    gaps = mcp.get_nutrition_gaps(day)
    assert set(gaps["missing_meals"]) == {"breakfast", "lunch", "dinner"}


def test_nutrition_gaps_skipped_meal_event_is_not_missing(db: Database) -> None:
    day = _day(5)
    db.add_health_event("skipped_meal", {"meal": "lunch"}, day=day)
    gaps = mcp.get_nutrition_gaps(day)
    assert "lunch" not in gaps["missing_meals"]
    assert "breakfast" in gaps["missing_meals"]


def test_nutrition_gaps_future_day_has_nothing_missing_yet(db: Database) -> None:
    future = (date.today() + timedelta(days=3)).isoformat()
    gaps = mcp.get_nutrition_gaps(future)
    assert gaps["missing_meals"] == []
    assert gaps["pre_workout_meal_missing"] is None
    assert gaps["post_workout_meal_missing"] is None


# ── Feature 2: merge_garmin_strength_fragments ──────────────────────────────────

def _seed_fragments(db: Database, day: str, starts: list[str]) -> list[int]:
    ids = []
    for i, start in enumerate(starts):
        aid = 500_000 + i
        db.upsert_workout(
            aid, day, name=f"Strength {i}", type="strength_training",
            duration_s=600, calories=60, avg_hr=120 + i, max_hr=140 + i,
            training_load=10.0 + i, source="garmin", load_source="garmin",
            start_time=start,
        )
        ids.append(aid)
    return ids


def test_merge_combines_close_fragments(db: Database) -> None:
    day = _day()
    ids = _seed_fragments(db, day, ["2026-07-04 18:00:00", "2026-07-04 18:15:00", "2026-07-04 18:35:00"])
    result = mcp.merge_garmin_strength_fragments(day)
    assert result["merged"] is True
    assert result["fragment_ids"] == sorted(ids)
    assert result["total_duration_s"] == 1800
    assert result["total_calories"] == 180
    assert result["training_load_before"] == 10.0 + 11.0 + 12.0

    workouts = db.recent_workouts(days=2)
    merged = [w for w in workouts if w["source"] == "garmin_merged"]
    assert len(merged) == 1
    assert merged[0]["activity_id"] == result["merged_activity_id"]

    # Fragments no longer count separately.
    assert db.latest_summary()["workout_count"] == 1


def test_merge_dry_run_does_not_change_data(db: Database) -> None:
    day = _day()
    _seed_fragments(db, day, ["2026-07-04 18:00:00", "2026-07-04 18:15:00"])
    result = mcp.merge_garmin_strength_fragments(day, dry_run=True)
    assert result["merged"] is False
    assert result["would_merge"] is True
    assert db.latest_summary()["workout_count"] == 2  # untouched
    assert all(w.get("duplicate_of") is None for w in db.recent_workouts(days=2))


def test_merge_is_idempotent(db: Database) -> None:
    day = _day()
    _seed_fragments(db, day, ["2026-07-04 18:00:00", "2026-07-04 18:15:00"])
    first = mcp.merge_garmin_strength_fragments(day)
    second = mcp.merge_garmin_strength_fragments(day)
    assert first["merged_activity_id"] == second["merged_activity_id"]
    merged_rows = [w for w in db.recent_workouts(days=2) if w["source"] == "garmin_merged"]
    assert len(merged_rows) == 1  # not duplicated on re-run


def test_merge_skips_when_too_few_fragments(db: Database) -> None:
    day = _day()
    _seed_fragments(db, day, ["2026-07-04 18:00:00"])
    result = mcp.merge_garmin_strength_fragments(day)
    assert result["merged"] is False
    assert "need at least" in result["reason"]


def test_merge_stays_idempotent_when_a_delayed_fragment_arrives(db: Database) -> None:
    """A 3rd fragment syncing in late (delayed watch upload) must extend the
    existing merged row instead of creating a second, double-counting one."""
    day = _day()
    _seed_fragments(db, day, ["2026-07-04 18:00:00", "2026-07-04 18:15:00"])
    first = mcp.merge_garmin_strength_fragments(day)

    db.upsert_workout(
        500_099, day, name="Strength late", type="strength_training",
        duration_s=600, calories=60, avg_hr=125, max_hr=145,
        training_load=13.0, source="garmin", load_source="garmin",
        start_time="2026-07-04 18:35:00",
    )
    second = mcp.merge_garmin_strength_fragments(day)
    assert second["merged_activity_id"] == first["merged_activity_id"]
    assert len(second["fragment_ids"]) == 3
    merged_rows = [w for w in db.recent_workouts(days=2) if w["source"] == "garmin_merged"]
    assert len(merged_rows) == 1  # not a second merged row
    assert db.latest_summary()["workout_count"] == 1  # no double counting


def test_merge_handles_two_qualifying_sessions_same_day(db: Database) -> None:
    day = _day()
    _seed_fragments(
        db, day,
        ["2026-07-04 07:00:00", "2026-07-04 07:15:00",  # morning session (2)
         "2026-07-04 19:00:00", "2026-07-04 19:15:00", "2026-07-04 19:35:00"],  # evening (3)
    )
    result = mcp.merge_garmin_strength_fragments(day)
    assert result["merged"] is True
    assert len(result["fragment_ids"]) == 3  # largest reported at top level
    assert len(result["other_merges"]) == 1
    assert result["other_merges"][0]["merged"] is True
    assert len(result["other_merges"][0]["fragment_ids"]) == 2

    merged_rows = [w for w in db.recent_workouts(days=2) if w["source"] == "garmin_merged"]
    assert len(merged_rows) == 2  # both sessions got their own canonical row
    assert db.latest_summary()["workout_count"] == 2


def test_merge_does_not_blend_distant_sessions_when_one_fragment_is_undated(db: Database) -> None:
    """A single fragment with no start_time must not force two genuinely
    separate, far-apart sessions into one merged group."""
    day = _day()
    ids = _seed_fragments(
        db, day,
        ["2026-07-04 07:00:00", "2026-07-04 07:15:00",  # morning session
         "2026-07-04 19:00:00", "2026-07-04 19:15:00"],  # evening session
    )
    db.upsert_workout(  # a 5th fragment with no timing info at all
        500_098, day, name="Strength unknown-time", type="strength_training",
        duration_s=600, source="garmin", load_source="garmin", training_load=10.0,
    )
    result = mcp.merge_garmin_strength_fragments(day)
    assert result["merged"] is True
    all_merged_ids = set(result["fragment_ids"])
    for other in result.get("other_merges", []):
        all_merged_ids |= set(other["fragment_ids"])
    # The two real, timed sessions are each merged (2 fragments each); the
    # untimed fragment is never folded into either one.
    assert all(fid in ids for fid in all_merged_ids)
    assert 500_098 not in all_merged_ids


def test_merge_splits_sessions_far_apart(db: Database) -> None:
    day = _day()
    _seed_fragments(
        db, day,
        ["2026-07-04 07:00:00", "2026-07-04 07:15:00",  # morning session
         "2026-07-04 19:00:00", "2026-07-04 19:15:00"],  # evening session
    )
    result = mcp.merge_garmin_strength_fragments(day, max_gap_minutes=90)
    assert result["merged"] is True
    assert len(result["fragment_ids"]) == 2  # only one group merged (largest)


def test_merge_ignores_non_strength_and_manual_workouts(db: Database) -> None:
    day = _day()
    db.upsert_workout(600001, day, type="running", duration_s=1800, source="garmin")
    db.upsert_workout(600002, day, type="strength_training", duration_s=600,
                       source="manual", start_time="2026-07-04 18:00:00")
    db.upsert_workout(600003, day, type="strength_training", duration_s=600,
                       source="garmin", start_time="2026-07-04 18:10:00")
    result = mcp.merge_garmin_strength_fragments(day)
    assert result["merged"] is False  # only one garmin strength fragment


# ── Feature 5: workout-log flow ─────────────────────────────────────────────────

from garmin_coach.workout_flow import (  # noqa: E402
    default_exercises,
    parse_completion,
    parse_duration_seconds,
    parse_exercise_reply,
)


def test_parse_completion_variants() -> None:
    assert parse_completion("Yes!") == "yes"
    assert parse_completion("done") == "yes"
    assert parse_completion("partially, only did half") == "partial"
    assert parse_completion("skipped, was too tired") == "skipped"
    assert parse_completion("banana") is None


def test_parse_completion_does_not_false_positive_on_substrings() -> None:
    # "gym" contains "y" (a _YES_WORDS entry) and "not sure" contains "no"
    # (a _SKIP_WORDS entry) as bare substrings — word-boundary matching must
    # not be fooled by either.
    assert parse_completion("gym was closed") is None
    assert parse_completion("not sure yet") is None


def test_parse_duration_seconds_variants() -> None:
    assert parse_duration_seconds("55") == 3300
    assert parse_duration_seconds("55 min") == 3300
    assert parse_duration_seconds("1 hour") == 3600
    assert parse_duration_seconds("1.5 hours") == 5400
    assert parse_duration_seconds("no idea") is None


def test_parse_exercise_reply_extracts_structured_fields() -> None:
    default = {"exercise_name": "Bench press", "planned_sets": 3, "planned_reps": "8"}
    ex = parse_exercise_reply("3x8 @60kg RPE7 no pain", default)
    assert ex["sets"] == 3 and ex["reps"] == 8
    assert ex["weight_kg"] == 60.0
    assert ex["rpe"] == 7.0
    assert ex["pain_note"] is None
    assert ex["exercise_name"] == "Bench press"
    assert ex["status"] == "completed"


def test_parse_exercise_reply_captures_pain_and_leftover_notes() -> None:
    ex = parse_exercise_reply("3x10 @40kg pain: left knee, felt tight", {"exercise_name": "Squat"})
    assert ex["pain_note"] == "left knee"
    assert "felt tight" in ex["notes"]


def test_parse_exercise_reply_skip_accepts_defaults() -> None:
    default = {"exercise_name": "Row", "planned_sets": 3, "planned_weight_kg": 50}
    ex = parse_exercise_reply("/skip", default)
    assert ex["planned_weight_kg"] == 50
    assert ex.get("weight_kg") is None
    assert ex["status"] == "completed"


def test_default_exercises_from_plan_shape() -> None:
    out = default_exercises([{"exercise_name": "Leg press", "sets": 3, "reps": 10, "weight_kg": 80}])
    assert out == [{
        "exercise_name": "Leg press", "machine": None,
        "planned_sets": 3, "planned_reps": "10", "planned_weight_kg": 80,
    }]


@pytest.fixture()
def flows(db: Database) -> WorkoutLogFlows:
    return WorkoutLogFlows(db)


def test_flow_full_happy_path_logs_strength_session(db: Database, flows: WorkoutLogFlows) -> None:
    plan = db.create_training_plan(
        _day(), title="Push day", workout_type="strength",
        exercises=[{"exercise_name": "Bench press", "planned_sets": 3, "planned_reps": "8"}],
    )
    started = flows.start(plan["id"])
    assert started["reused"] is False
    flow_id = started["flow_id"]

    r1 = flows.handle_reply(flow_id, "yes")
    assert r1["finished"] is False
    r2 = flows.handle_reply(flow_id, "55")
    assert r2["finished"] is False
    assert "Bench press" in r2["reply"]
    r3 = flows.handle_reply(flow_id, "3x8 @60kg RPE7 no pain")
    assert r3["finished"] is True

    result = r3["result"]
    assert result["status"] == "done"
    assert result["duration_s"] == 3300
    assert result["exercises_logged"] == 1
    assert result["strength_session_id"] is not None

    updated_plan = db.get_training_plan(plan["id"])
    assert updated_plan["status"] == "done"
    assert updated_plan["actual_duration_s"] == 3300

    session = db.get_strength_session(result["strength_session_id"])
    assert session["exercises"][0]["weight_kg"] == 60.0
    assert session["exercises"][0]["rpe"] == 7.0


def test_flow_skip_records_reason_and_marks_plan_skipped(db: Database, flows: WorkoutLogFlows) -> None:
    plan = db.create_training_plan(_day(), title="Leg day")
    flow_id = flows.start(plan["id"])["flow_id"]
    flows.handle_reply(flow_id, "skipped")
    outcome = flows.handle_reply(flow_id, "too sore")
    assert outcome["finished"] is True
    assert outcome["result"]["status"] == "skipped"
    assert outcome["result"]["skip_reason"] == "too sore"
    assert db.get_training_plan(plan["id"])["status"] == "skipped"


def test_flow_cancel_leaves_plan_untouched(db: Database, flows: WorkoutLogFlows) -> None:
    plan = db.create_training_plan(_day(), title="Leg day")
    flow_id = flows.start(plan["id"])["flow_id"]
    flows.handle_reply(flow_id, "yes")
    outcome = flows.handle_reply(flow_id, "/cancel")
    assert outcome["result"]["cancelled"] is True
    assert db.get_training_plan(plan["id"])["status"] == "planned"


def test_flow_skip_duration_uses_plan_estimate(db: Database, flows: WorkoutLogFlows) -> None:
    plan = db.create_training_plan(_day(), title="Row", estimated_duration_s=1800)
    flow_id = flows.start(plan["id"])["flow_id"]
    flows.handle_reply(flow_id, "yes")
    outcome = flows.handle_reply(flow_id, "/skip")
    assert outcome["finished"] is True  # no exercises planned → finishes here
    assert outcome["result"]["duration_s"] == 1800


def test_flow_done_early_stops_exercise_collection(db: Database, flows: WorkoutLogFlows) -> None:
    plan = db.create_training_plan(
        _day(), title="Full body",
        exercises=[
            {"exercise_name": "Squat"}, {"exercise_name": "Bench"}, {"exercise_name": "Row"},
        ],
    )
    flow_id = flows.start(plan["id"])["flow_id"]
    flows.handle_reply(flow_id, "yes")
    flows.handle_reply(flow_id, "50")
    flows.handle_reply(flow_id, "3x8 @100kg")  # Squat
    outcome = flows.handle_reply(flow_id, "/done")  # stop before Bench/Row
    assert outcome["finished"] is True
    assert outcome["result"]["exercises_logged"] == 1


def test_flow_tolerates_unparseable_replies_and_asks_again(db: Database, flows: WorkoutLogFlows) -> None:
    plan = db.create_training_plan(_day(), title="Leg day")
    flow_id = flows.start(plan["id"])["flow_id"]
    outcome = flows.handle_reply(flow_id, "banana")
    assert outcome["finished"] is False
    assert flows.get(flow_id)["step"] == "awaiting_completion"


def test_flow_start_is_idempotent_while_open(db: Database, flows: WorkoutLogFlows) -> None:
    plan = db.create_training_plan(_day(), title="Leg day")
    first = flows.start(plan["id"])
    second = flows.start(plan["id"])
    assert second["reused"] is True
    assert second["flow_id"] == first["flow_id"]


def test_flow_unknown_plan_raises(db: Database, flows: WorkoutLogFlows) -> None:
    with pytest.raises(ValueError):
        flows.start(999_999)


def test_flow_starting_for_a_new_plan_supersedes_the_orphaned_one(
    db: Database, flows: WorkoutLogFlows
) -> None:
    """Only one Telegram conversation can be active at a time — starting a
    flow for plan B while plan A's is still open must close A out instead of
    leaving it silently unreachable forever."""
    plan_a = db.create_training_plan(_day(), title="Leg day")
    plan_b = db.create_training_plan(_day(), title="Push day")

    flow_a = flows.start(plan_a["id"])["flow_id"]
    flow_b = flows.start(plan_b["id"])["flow_id"]

    assert flows.get(flow_a)["completed_at"] is not None  # superseded, not orphaned
    assert flows.get(flow_b)["completed_at"] is None
    assert flows.active_flow()["id"] == flow_b


def test_start_workout_log_flow_tool_pushes_prompt(db: Database, monkeypatch) -> None:
    plan = db.create_training_plan(_day(), title="Leg day")
    sent = {}

    def fake_send(message: str) -> int:
        sent["message"] = message
        return 42

    monkeypatch.setattr(mcp, "send_telegram_message", fake_send)
    result = mcp.start_workout_log_flow(plan["id"])
    assert result["flow_id"] is not None
    assert "complete the workout" in sent["message"]


# ── Telegram routing: an open flow claims /cancel /skip /done before their
# normal (unrelated) command handlers would ──────────────────────────────────

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from telegram.ext import ApplicationHandlerStop  # noqa: E402

from garmin_coach.telegram_bot import TelegramCoach  # noqa: E402


def _text_update(text: str, chat_id: int = 12345):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    chat = SimpleNamespace(id=chat_id)
    return SimpleNamespace(message=message, effective_chat=chat)


def test_route_active_flow_intercepts_when_open(db: Database) -> None:
    import asyncio

    bot = TelegramCoach(db)
    bot._allowed = "12345"
    plan = db.create_training_plan(_day(), title="Leg day")
    bot.workout_flows.start(plan["id"])

    update = _text_update("yes")
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot.route_active_flow(update, None))
    update.message.reply_text.assert_awaited()


def test_route_active_flow_passthrough_when_no_flow(db: Database) -> None:
    import asyncio

    bot = TelegramCoach(db)
    bot._allowed = "12345"
    update = _text_update("/done workout")
    asyncio.run(bot.route_active_flow(update, None))  # must not raise or reply
    update.message.reply_text.assert_not_awaited()
