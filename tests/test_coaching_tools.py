"""Offline tests for the coaching upgrade: profile/goals, strength logging,
training plans + adherence, nutrition summary, edit/delete tools, workout
deduplication, training-load fallback, readiness, body measurements, and
hydration trends."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

import garmin_coach.mcp_server as mcp
from garmin_coach.database import Database
from garmin_coach.training_load import estimate_training_load


@pytest.fixture()
def db(tmp_path, monkeypatch) -> Database:
    database = Database(path=str(tmp_path / "coaching.db"))
    # The MCP tools are thin wrappers over module-level handles — point them
    # at the test database so tool-level behaviour can be exercised directly.
    monkeypatch.setattr(mcp, "db", database)
    return database


def _day(offset: int = 0) -> str:
    return (date.today() - timedelta(days=offset)).isoformat()


# ── Priority 1: profile / goals ─────────────────────────────────────────────────

def test_profile_missing_returns_none(db: Database) -> None:
    assert db.get_profile() is None


def test_profile_partial_update_preserves_other_fields(db: Database) -> None:
    db.set_profile(age=38, goal_type="fat_loss", calorie_target=2200)
    db.set_profile(protein_target_g=150.0)  # partial — must not wipe the rest
    profile = db.get_profile()
    assert profile["age"] == 38
    assert profile["goal_type"] == "fat_loss"
    assert profile["calorie_target"] == 2200
    assert profile["protein_target_g"] == 150.0


def test_profile_replace_rewrites_everything(db: Database) -> None:
    db.set_profile(age=38, goal_type="fat_loss", notes="old")
    db.set_profile(replace=True, goal_type="muscle_gain")
    profile = db.get_profile()
    assert profile["goal_type"] == "muscle_gain"
    assert profile["age"] is None
    assert profile["notes"] is None


# ── Priority 2: strength sessions ───────────────────────────────────────────────

_FULL_BODY = [
    {"exercise_name": "Leg press", "sets": 3, "reps": 10, "weight_kg": 80, "rpe": 7},
    {"exercise_name": "Dumbbell bench press", "sets": 3, "reps": 10, "weight_kg": 20, "rpe": 7},
]


def test_strength_session_full_and_partial_exercises(db: Database) -> None:
    session = db.add_strength_session(
        _day(), exercises=_FULL_BODY, session_name="Full body strength",
        duration_s=3600,
    )
    assert session["id"] is not None
    assert len(session["exercises"]) == 2
    assert session["exercises"][0]["weight_kg"] == 80

    # Partial exercise data (name only) is accepted.
    partial = db.add_strength_session(
        _day(), exercises=[{"exercise_name": "Plank"}], session_name="Core"
    )
    assert partial["exercises"][0]["sets"] is None


def test_strength_session_visible_in_workout_history_with_load(db: Database) -> None:
    session = db.add_strength_session(
        _day(), exercises=_FULL_BODY, session_name="Full body", duration_s=3600
    )
    workouts = db.recent_workouts(days=2)
    linked = [w for w in workouts if w["activity_id"] == session["activity_id"]]
    assert len(linked) == 1
    assert linked[0]["type"] == "strength_training"
    assert linked[0]["training_load"] > 0  # RPE-based estimate
    assert linked[0]["load_source"] == "estimated"
    assert db.latest_summary()["workout_count"] == 1


def test_exercise_history_tracks_progressive_overload(db: Database) -> None:
    db.add_strength_session(
        _day(7),
        exercises=[{"exercise_name": "Leg press", "sets": 3, "reps": 10, "weight_kg": 75, "rpe": 8}],
    )
    db.add_strength_session(
        _day(0),
        exercises=[{"exercise_name": "Leg press", "sets": 3, "reps": 10, "weight_kg": 80, "rpe": 7}],
    )
    history = db.exercise_history("leg press")  # case-insensitive
    assert [h["weight_kg"] for h in history] == [80, 75]  # newest first
    assert history[0]["estimated_volume_kg"] == 2400.0
    assert history[0]["rpe"] == 7


def test_strength_session_update_and_delete(db: Database) -> None:
    session = db.add_strength_session(
        _day(), exercises=_FULL_BODY, session_name="Full body", duration_s=3600
    )
    updated = db.update_strength_session(
        session["id"],
        exercises=[{"exercise_name": "Leg press", "sets": 4, "reps": 8, "weight_kg": 85}],
        duration_s=2700.0,
    )
    assert updated["duration_s"] == 2700.0
    assert len(updated["exercises"]) == 1  # replaced, not appended

    assert db.delete_strength_session(session["id"]) is True
    assert db.get_strength_session(session["id"]) is None
    assert db.recent_workouts(days=2) == []  # mirrored workout gone too
    # Summaries survive the deletion.
    assert db.latest_summary() is None or db.latest_summary().get("workout_count") in (0, None)


# ── Exercise-level logging: per-set data, planned vs actual, substitutions ─────

_LEG_PRESS_SETS = [
    {"reps": 10, "weight_kg": 80, "rpe": 7},
    {"reps": 9, "weight_kg": 80, "rpe": 7.5},
    {"reps": 8, "weight_kg": 80, "rpe": 8},
]


def test_per_set_logging_derives_aggregates_and_roundtrips(db: Database) -> None:
    session = db.add_strength_session(
        _day(),
        gym="Sports Center",
        session_name="Full Body Hypertrophy Restart",
        exercises=[{
            "exercise_name": "Leg Press",
            "machine": "45° sled",
            "planned_sets": 3,
            "planned_reps": "8-10",
            "planned_weight_kg": 80,
            "actual_sets": _LEG_PRESS_SETS,
            "status": "completed",
            "notes": "Good form, keep same weight next time",
        }],
    )
    assert session["gym"] == "Sports Center"
    ex = session["exercises"][0]
    assert ex["actual_sets"] == _LEG_PRESS_SETS  # per-set data preserved
    assert ex["sets"] == 3  # aggregates derived from the sets
    assert ex["reps"] == 9
    assert ex["weight_kg"] == 80
    assert ex["rpe"] == 7.5
    assert ex["planned_reps"] == "8-10"

    history = db.exercise_history("leg press")[0]
    assert history["estimated_volume_kg"] == (10 + 9 + 8) * 80
    assert history["best_set_weight_kg"] == 80
    assert history["machine"] == "45° sled"
    assert history["gym"] == "Sports Center"


def test_skipped_and_substituted_exercises_visible(db: Database) -> None:
    session = db.add_strength_session(
        _day(),
        exercises=[
            {"exercise_name": "Bench press", "status": "substituted",
             "substitute_exercise": "Dumbbell bench press",
             "notes": "bench was busy"},
            {"exercise_name": "Lat pulldown", "completed": False},  # → skipped
            {"exercise_name": "Leg press", "sets": 3, "reps": 10, "weight_kg": 80},
        ],
    )
    statuses = {e["exercise_name"]: e["status"] for e in session["exercises"]}
    assert statuses["Bench press"] == "substituted"
    assert statuses["Lat pulldown"] == "skipped"  # derived from completed=False
    assert session["exercises"][0]["substitute_exercise"] == "Dumbbell bench press"


# ── Next-session weight recommendations ─────────────────────────────────────────

def test_recommendation_progresses_when_rpe_low(db: Database) -> None:
    db.add_strength_session(
        _day(3),
        exercises=[{"exercise_name": "Leg Press", "actual_sets": _LEG_PRESS_SETS}],
    )
    from garmin_coach.progression import recommend_next_weight

    rec = recommend_next_weight(db.exercise_history("Leg Press")[0])
    # Aggregate RPE 7.5 → in the maintain zone: add reps before load.
    assert rec["action"] == "maintain"
    assert rec["recommended_weight_kg"] == 80

    db.add_strength_session(
        _day(1),
        exercises=[{"exercise_name": "Leg Press", "sets": 3, "reps": 10,
                    "weight_kg": 80, "rpe": 6.5}],
    )
    rec = recommend_next_weight(db.exercise_history("Leg Press")[0])
    assert rec["action"] == "increase"
    assert rec["recommended_weight_kg"] == 82.5  # one plate step


def test_recommendation_backs_off_on_high_rpe_or_pain() -> None:
    from garmin_coach.progression import recommend_next_weight

    grinding = recommend_next_weight({"best_set_weight_kg": 100, "rpe": 9})
    assert grinding["action"] == "reduce"
    assert grinding["recommended_weight_kg"] == 95.0

    hurting = recommend_next_weight(
        {"best_set_weight_kg": 60, "rpe": 6, "pain_note": "left knee"}
    )
    assert hurting["action"] == "reduce"

    unknown = recommend_next_weight({"weight_kg": None})
    assert unknown["action"] == "log_first"
    assert unknown["recommended_weight_kg"] is None


def test_recovery_caution_blocks_increases(db: Database, monkeypatch) -> None:
    db.add_strength_session(
        _day(2),
        exercises=[{"exercise_name": "Squat", "sets": 3, "reps": 8,
                    "weight_kg": 100, "rpe": 6}],
    )
    db.upsert_readiness(_day(), soreness_1_10=8, energy_1_10=3)
    db.upsert_sleep(_day(), score=70, total_seconds=6 * 3600)

    from garmin_coach.analysis import Analyzer
    monkeypatch.setattr(mcp, "analyzer", Analyzer(db))

    result = mcp.recommend_next_weights(exercises=["Squat"])
    assert "soreness" in result["recovery_caution"]
    rec = result["recommendations"][0]
    # RPE 6 would earn an increase, but recovery gates it.
    assert rec["action"] == "maintain"
    assert rec["recommended_weight_kg"] == 100
    assert "recovery says hold" in rec["reason"]


def test_recommend_covers_recently_trained_when_unspecified(db: Database, monkeypatch) -> None:
    db.add_strength_session(
        _day(5),
        exercises=[
            {"exercise_name": "Leg Press", "weight_kg": 80, "sets": 3, "reps": 10, "rpe": 6},
            {"exercise_name": "Row", "weight_kg": 50, "sets": 3, "reps": 10, "rpe": 8},
        ],
    )
    from garmin_coach.analysis import Analyzer
    monkeypatch.setattr(mcp, "analyzer", Analyzer(db))

    result = mcp.recommend_next_weights()
    by_name = {r["exercise"]: r for r in result["recommendations"]}
    assert result["recovery_caution"] is None
    assert by_name["Leg Press"]["action"] == "increase"
    assert by_name["Row"]["action"] == "maintain"


def test_stale_readiness_does_not_gate_progress() -> None:
    from garmin_coach.progression import recovery_caution

    report = {
        "available": True,
        "flags": [],
        "readiness": {"day": _day(5), "soreness_1_10": 9},  # a week old
    }
    assert recovery_caution(report) is None


# ── Priority 3: training plans and adherence ────────────────────────────────────

def test_training_plan_lifecycle(db: Database) -> None:
    plan = db.create_training_plan(
        _day(), title="Upper body + zone 2", workout_type="strength",
        exercises=[{"exercise_name": "Bench", "sets": 3, "reps": 8}],
        estimated_duration_s=3000,
    )
    assert plan["status"] == "planned"
    assert plan["exercises"][0]["exercise_name"] == "Bench"

    today = db.get_today_training_plans()
    assert len(today) == 1 and today[0]["id"] == plan["id"]

    done = db.update_training_plan(
        plan["id"], status="done", actual_duration_s=3300.0, difficulty_rpe=7.5
    )
    assert done["status"] == "done"
    assert done["difficulty_rpe"] == 7.5

    skipped = db.create_training_plan(_day(), title="Evening walk")
    db.update_training_plan(skipped["id"], status="skipped", skip_reason="work ran late")
    only_skipped = db.get_training_plans(days=7, status="skipped")
    assert [p["skip_reason"] for p in only_skipped] == ["work ran late"]


# ── Priority 4: nutrition summary ───────────────────────────────────────────────

def test_nutrition_summary_mixed_meals_and_targets(db: Database) -> None:
    db.set_profile(calorie_target=2200, protein_target_g=150.0)
    db.add_meal("toast", day=_day(), calories=300)  # calorie-only
    db.add_meal("chicken bowl", day=_day(), calories=650, protein_g=45.0, carbs_g=60.0)

    summary = db.nutrition_summary(day=_day())[0]
    assert summary["total_calories"] == 950
    assert summary["total_protein_g"] == 45.0  # calorie-only meal contributes nothing
    assert summary["meal_count"] == 2
    assert summary["calories_remaining"] == 1250
    assert summary["protein_remaining_g"] == 105.0
    assert len(summary["meals"]) == 2


def test_nutrition_summary_without_profile_targets(db: Database) -> None:
    db.add_meal("toast", day=_day(), calories=300)
    summary = db.nutrition_summary(day=_day())[0]
    assert summary["calorie_target"] is None
    assert summary["calories_remaining"] is None  # missing targets never crash


# ── Priority 5: edit/delete ─────────────────────────────────────────────────────

def test_meal_update_and_delete_via_tools(db: Database) -> None:
    db.add_meal("hummus plate", day=_day(), calories=900)
    meal_id = db.meals_for_day(_day())[0]["id"]

    result = mcp.update_meal(meal_id, calories=750, protein_g=25.0)
    assert result["updated"] is True
    assert result["meals"][0]["calories"] == 750
    assert result["meals"][0]["name"] == "hummus plate"  # partial update

    result = mcp.delete_meal(meal_id)
    assert result["deleted"] is True and result["meals"] == []
    assert db.daily_summary(days=1) == [] or db.daily_summary(days=1)[0]["calories_in"] == 0

    assert "error" in mcp.delete_meal(999_999)


def test_workout_update_and_delete_via_tools(db: Database) -> None:
    result = mcp.log_workout(name="Run", type="running", duration_s=1800, day=_day())
    activity_id = result["activity_id"]

    updated = mcp.update_workout(activity_id, distance_m=5200.0, training_load=90.0)
    workout = updated["workouts"][0]
    assert workout["distance_m"] == 5200.0
    assert workout["training_load"] == 90.0
    assert workout["load_source"] == "manual"  # explicit override recorded

    deleted = mcp.delete_workout(activity_id)
    assert deleted["deleted"] is True and deleted["workouts"] == []


# ── Priority 6: deduplication ───────────────────────────────────────────────────

def _seed_duplicate_pair(db: Database) -> tuple[int, int]:
    """The observed case: one walk recorded by Garmin and again by an import."""
    db.upsert_workout(
        111, _day(), name="הליכה", type="walking", duration_s=2400, distance_m=3000,
        calories=180, training_load=25.0, source="garmin", load_source="garmin",
    )
    db.upsert_workout(
        -5000, _day(), name="Walking", type="walking", duration_s=2450, distance_m=3050,
        calories=190, source="apple",
    )
    return 111, -5000


def test_duplicate_detection_finds_the_pair(db: Database) -> None:
    _seed_duplicate_pair(db)
    # A genuinely different workout must not join the group.
    db.upsert_workout(222, _day(), name="Run", type="running",
                      duration_s=1500, distance_m=5000, source="garmin")
    groups = db.find_duplicate_workouts(days=7)
    assert len(groups) == 1
    assert {w["activity_id"] for w in groups[0]} == {111, -5000}


def test_dedupe_keeps_garmin_and_summaries_ignore_duplicates(db: Database) -> None:
    garmin_id, apple_id = _seed_duplicate_pair(db)
    before = db.latest_summary()
    assert before["workout_count"] == 2  # both counted pre-dedupe

    result = db.dedupe_workouts(days=7)
    assert result["marked"][0]["kept"] == garmin_id
    assert result["marked"][0]["marked_duplicate"] == apple_id

    after = db.latest_summary()
    assert after["workout_count"] == 1
    assert after["training_load"] == 25.0  # apple estimate no longer double-counts
    assert db.recent_workouts(days=7, include_duplicates=True)[0] is not None
    assert len(db.recent_workouts(days=7)) == 1  # default read hides duplicates


def test_source_priority_can_prefer_manual(db: Database) -> None:
    db.set_profile(activity_source_priority="manual,garmin,apple")
    garmin_id, apple_id = _seed_duplicate_pair(db)
    db.upsert_workout(
        -6000, _day(), name="My walk", type="walking", duration_s=2420,
        distance_m=3020, calories=185, source="manual",
    )
    result = db.dedupe_workouts(days=7)
    assert all(m["kept"] == -6000 for m in result["marked"])


# ── Priority 7: training-load fallback ──────────────────────────────────────────

def test_estimator_separates_walking_from_running_and_strength() -> None:
    walk = estimate_training_load("walking", 45 * 60)
    run = estimate_training_load("running", 30 * 60, avg_hr=155)
    lift = estimate_training_load("strength_training", 60 * 60, rpe=7)
    assert walk is not None and run is not None and lift is not None
    assert walk < lift and walk < run
    assert estimate_training_load("running", None) is None  # nothing to go on


def test_training_load_not_zero_for_unlabelled_workouts(db: Database) -> None:
    # Real workouts whose training_load Garmin never provided.
    for i in range(7):
        db.upsert_workout(
            1000 + i, _day(i), name="Walk", type="walking", duration_s=3600,
            training_load=estimate_training_load("walking", 3600),
            source="garmin", load_source="estimated",
        )
    db.upsert_steps(_day(27), steps=100)  # anchor the ACR history span
    from garmin_coach.analysis import Analyzer

    acr = Analyzer(db).acute_chronic_ratio()
    assert acr["acute_7d"] > 0  # previously read as complete rest


def test_garmin_provided_load_is_preserved_on_repull(db: Database) -> None:
    db.upsert_workout(333, _day(), type="running", duration_s=1800,
                      training_load=estimate_training_load("running", 1800),
                      load_source="estimated", source="garmin")
    # Later Garmin re-pull carries the real load.
    db.upsert_workout(333, _day(), type="running", duration_s=1800,
                      training_load=71.0, load_source="garmin", source="garmin")
    workout = db.recent_workouts(days=2)[0]
    assert workout["training_load"] == 71.0
    assert workout["load_source"] == "garmin"


# ── Priority 8: readiness ───────────────────────────────────────────────────────

def test_readiness_roundtrip_and_report_inclusion(db: Database) -> None:
    db.upsert_readiness(_day(), energy_1_10=6, soreness_1_10=7, mood="tired",
                        pain_areas="left knee")
    rows = db.recent_readiness(days=7)
    assert rows[-1]["soreness_1_10"] == 7

    from garmin_coach.analysis import Analyzer

    db.upsert_sleep(_day(), score=80, total_seconds=7 * 3600)
    report = Analyzer(db).report()
    assert report["readiness"]["pain_areas"] == "left knee"


def test_missing_readiness_breaks_nothing(db: Database) -> None:
    from garmin_coach.analysis import Analyzer

    db.upsert_sleep(_day(), score=80, total_seconds=7 * 3600)
    assert Analyzer(db).report()["readiness"] is None


# ── Priority 9: body measurements ───────────────────────────────────────────────

def test_body_measurement_trend_deltas(db: Database) -> None:
    db.upsert_body_measurement(_day(14), waist_cm=94.0, arm_cm=36.0)
    db.upsert_body_measurement(_day(0), waist_cm=92.5)  # arm not re-measured

    trend = mcp.get_body_measurement_trend(days=30)
    assert trend["deltas"]["waist_cm"]["delta"] == -1.5
    assert trend["deltas"]["arm_cm"]["previous"] is None  # single point, no delta
    assert "chest_cm" not in trend["deltas"]  # never measured → absent, not crash


# ── On-demand Garmin sync + sync status ─────────────────────────────────────────

class _FakeGarmin:
    def __init__(self, db, results=None, error=None):
        self.db = db
        self.results = results or {"sleep": "ok", "steps": "ok"}
        self.error = error
        self.pulled: list[str] = []

    def pull_day(self, day):
        if self.error:
            raise self.error
        self.pulled.append(day)
        self.db.record_pull(day, self.results)
        return self.results


def test_sync_status_before_any_pull(db: Database) -> None:
    assert mcp.get_sync_status()["synced_ever"] is False


def test_sync_garmin_pulls_and_status_reflects_it(db: Database, monkeypatch) -> None:
    fake = _FakeGarmin(db)
    monkeypatch.setattr(mcp, "_garmin", fake)

    result = mcp.sync_garmin()
    assert result["synced"] is True
    assert result["day"] == _day()
    assert result["metrics_ok"] == 2
    assert fake.pulled == [_day()]

    status = mcp.get_sync_status()
    assert status["synced_ever"] is True
    assert status["last_synced_day"] == _day()
    assert status["minutes_since_last_sync"] < 1
    assert status["stale"] is False
    assert status["last_results"] == {"sleep": "ok", "steps": "ok"}


def test_sync_throttle_uses_intervals_and_force_and_specific_day_override(
    db: Database, monkeypatch
) -> None:
    fake = _FakeGarmin(db)
    monkeypatch.setattr(mcp, "_garmin", fake)

    # First sync always runs (nothing pulled yet).
    assert mcp.sync_garmin()["synced"] is True
    assert fake.pulled == [_day()]

    # 30 min later: inside the 60-min default interval → throttled with a
    # structured skip result; but an explicit force (10-min floor) runs.
    monkeypatch.setattr(mcp, "_minutes_since", lambda ts: 30.0)
    throttled = mcp.sync_garmin()
    assert throttled["synced"] is False
    assert throttled["reason"] == "throttled_recent_sync"
    assert throttled["minutes_since_last_sync"] == 30.0
    assert throttled["last_synced_at"] is not None
    assert fake.pulled == [_day()]  # no second hit on Garmin
    assert mcp.sync_garmin(force=True)["synced"] is True

    # 5 min later: even force is inside its own 10-min floor → throttled.
    monkeypatch.setattr(mcp, "_minutes_since", lambda ts: 5.0)
    assert mcp.sync_garmin(force=True)["synced"] is False

    # An explicit day is a deliberate backfill and always bypasses throttling.
    assert mcp.sync_garmin(day=_day(3))["synced"] is True
    assert _day(3) in fake.pulled


def test_sync_failure_is_reported_not_raised(db: Database, monkeypatch) -> None:
    monkeypatch.setattr(mcp, "_garmin", _FakeGarmin(db, error=RuntimeError("401 unauthorized")))
    result = mcp.sync_garmin(force=True)
    assert result["synced"] is False
    assert result["reason"] == "sync_failed"
    assert "401" in result["error"]
    assert "garmin_login" in result["hint"]


# ── Priority 10: hydration ──────────────────────────────────────────────────────

def test_hydration_trend_averages_and_missed_days(db: Database) -> None:
    db.upsert_hydration(_day(2), intake_ml=2000, goal_ml=2500)
    db.upsert_hydration(_day(1), intake_ml=1500, goal_ml=2500)

    trend = mcp.get_hydration_trend(days=3)
    assert trend["average_intake_ml"] == 1750
    assert trend["average_percent_of_goal"] == 70.0
    assert trend["days_logged"] == 2
    assert trend["missed_days"] == [_day(0)]  # today unlogged
    assert trend["entries"][0]["percent_of_goal"] == 80.0


def test_hydration_trend_empty_is_safe(db: Database) -> None:
    trend = mcp.get_hydration_trend(days=7)
    assert trend["average_intake_ml"] is None
    assert len(trend["missed_days"]) == 7
