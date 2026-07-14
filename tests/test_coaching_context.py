"""Tests for the unified daily coaching context, configurable sleep/hydration
targets, and nutrition provenance/import — the Priority-0 upgrade.

All offline: seed a temp SQLite DB and exercise the pure decision functions plus
the assembled context. No network / OpenAI / Garmin.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from garmin_coach.analysis import Analyzer
from garmin_coach.apple_health import import_export
from garmin_coach.coaching_context import (
    build_coaching_context,
    build_recommendation,
    classify_recovery,
    detect_workout_quality_warnings,
)
from garmin_coach.database import Database


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "ctx.db"))


def _seed(db: Database, days: int = 30, sleep_hours: float = 7.0) -> None:
    for i in range(days - 1, -1, -1):
        d = date.today() - timedelta(days=i)
        db.upsert_sleep(d, score=82, total_seconds=int(sleep_hours * 3600), resting_hr=50)
        db.upsert_hrv(d, last_night_avg=60, weekly_avg=60, status="BALANCED")
        db.upsert_resting_hr(d, resting_hr=50)
        db.upsert_stress(d, avg_stress=35)
        db.upsert_body_battery(d, high=85, low=20)
        db.upsert_steps(d, steps=8000, goal=10000)
        db.upsert_weight(d, weight_kg=77.0, body_fat=18.0)
        db.upsert_workout(2000 + i, d, name="Easy run", type="running", training_load=45)


# ── 1. Sleep debt against the configured 7h target ──────────────────────────────

def test_sleep_debt_uses_configured_seven_hour_target(db: Database) -> None:
    # Six hours every night → 1h debt/night vs the 7h baseline → 7h over a week.
    _seed(db, days=10, sleep_hours=6.0)
    analyzer = Analyzer(db)
    rows = db.daily_summary(days=28)
    assert db.sleep_target_for() == 7.0  # default baseline, never 8
    assert analyzer.sleep_debt(rows) == pytest.approx(7.0, abs=0.1)


def test_sleep_debt_clamped_per_night(db: Database) -> None:
    # A long night does not "pay back" a short one: debt is max(0, target-actual).
    _seed(db, days=7, sleep_hours=8.5)  # above 7h target every night → zero debt
    rows = db.daily_summary(days=28)
    assert Analyzer(db).sleep_debt(rows) == 0.0


# ── 2. Historical sleep calc stays reproducible after a target change ────────────

def test_historical_sleep_debt_after_target_change(db: Database) -> None:
    _seed(db, days=10, sleep_hours=7.0)
    analyzer = Analyzer(db)
    rows = db.daily_summary(days=28)

    # Baseline 7h target → steady 7h sleep = no debt.
    assert analyzer.sleep_debt(rows) == 0.0

    # User raises their target to 8h effective today; past nights keep the 7h
    # target (effective-dated), so only nights on/after today count against 8h.
    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=6)).isoformat()
    db.set_sleep_target(7.0, effective_from=week_ago)  # historical anchor
    db.set_sleep_target(8.0, effective_from=today)

    # Per-night resolution: only today's night is under the new 8h target.
    debt = analyzer.sleep_debt(rows)
    assert debt == pytest.approx(1.0, abs=0.1)  # one night × (8-7)

    # And an old day still resolves to the 7h target it had then.
    assert db.sleep_target_for(week_ago) == 7.0
    assert db.sleep_target_for(today) == 8.0


# ── Recovery classification (pure) ──────────────────────────────────────────────

def _base_signals(**over):
    args = dict(
        sleep_hours=7.5, sleep_score=85, hrv=60, hrv_status="BALANCED",
        resting_hr=50, resting_hr_delta=0, avg_stress=30, body_battery=80,
        acr_ratio=1.0, readiness=None, sleep_target_hours=7.0,
        sleep_minimum_recovery_hours=6.0,
    )
    args.update(over)
    return args


def test_recovery_good_when_all_signals_normal() -> None:
    r = classify_recovery(**_base_signals())
    assert r["status"] == "good"
    assert r["confidence"] > 0.8


def test_recovery_moderate_on_short_sleep_only() -> None:
    # Short sleep alone should reduce, not cancel — moderate, not compromised.
    r = classify_recovery(**_base_signals(sleep_hours=5.3, sleep_score=60))
    assert r["status"] == "moderate"
    assert any("5.3h" in reason and "below" in reason for reason in r["reasons"])


def test_recovery_low_when_multiple_markers_off() -> None:
    r = classify_recovery(**_base_signals(
        sleep_hours=5.0, hrv_status="UNBALANCED", resting_hr_delta=7, avg_stress=70,
    ))
    assert r["status"] in ("low", "compromised")


def test_recovery_compromised_on_reported_pain() -> None:
    r = classify_recovery(**_base_signals(readiness={"day": date.today().isoformat(),
                                                     "pain_areas": "left knee"}))
    assert r["status"] == "compromised"


def test_recovery_confidence_low_when_no_signals() -> None:
    r = classify_recovery(**_base_signals(
        sleep_hours=None, sleep_score=None, hrv=None, hrv_status=None,
        resting_hr=None, resting_hr_delta=None, avg_stress=None,
        body_battery=None, acr_ratio=None,
    ))
    assert r["confidence"] <= 0.3


# ── Recommendation (pure) reflects the training state ladder ─────────────────────

def test_recommendation_normal_training_when_recovered() -> None:
    rec = build_recommendation(
        recovery={"status": "good"}, profile={"protein_target_g": 150},
        sleep_hours=7.5, sleep_target_hours=7.0,
        hydration_targets={"baseline_ml": 2750, "training_day_ml": 3250},
        is_training_day=True, pending_plan={"id": 5, "title": "Full body A"},
    )
    assert rec["training_decision"] == "normal_strength"
    assert rec["hydration_target_ml"] == 3250  # training day
    assert rec["suggested_session"]["planned_plan_id"] == 5
    assert any("protein" in p for p in rec["nutrition_priorities"])


def test_recommendation_reduced_on_moderate_recovery() -> None:
    rec = build_recommendation(
        recovery={"status": "moderate"}, profile={},
        sleep_hours=5.3, sleep_target_hours=7.0,
        hydration_targets={"baseline_ml": 2750, "training_day_ml": 3250},
        is_training_day=True, pending_plan=None,
    )
    assert rec["training_decision"] == "reduced_strength"
    assert "short sleep" in rec["top_priority"]


def test_recommendation_rest_when_compromised() -> None:
    rec = build_recommendation(
        recovery={"status": "compromised"}, profile={},
        sleep_hours=4.0, sleep_target_hours=7.0,
        hydration_targets={"baseline_ml": 2750, "training_day_ml": 3250},
        is_training_day=True, pending_plan=None,
    )
    assert rec["training_decision"] == "rest"
    assert rec["hydration_target_ml"] == 2750  # not a training day's target when resting


# ── Unified context assembly (scheduled-automation entry point) ─────────────────

def test_scheduled_automation_retrieves_full_context(db: Database) -> None:
    _seed(db, days=30, sleep_hours=6.5)
    db.set_profile(protein_target_g=150, calorie_target=2400,
                   preferred_training_days="sunday,tuesday,thursday")
    ctx = build_coaching_context(db, include_recommendation=True)

    # Every required section is present.
    for section in ("day", "timezone", "data_freshness", "profile", "recovery",
                    "sleep", "activity", "training_load", "recent_workouts",
                    "strength_history", "body_composition", "nutrition",
                    "hydration", "pending_training_plan", "flags",
                    "data_quality_warnings", "recommendation"):
        assert section in ctx

    assert ctx["recovery"]["status"] in ("good", "moderate", "low", "compromised")
    assert ctx["sleep"]["target_hours"] == 7.0
    assert "training_decision" in ctx["recommendation"]
    # Sources are reported so a caller sees what was retrieved vs missing.
    assert ctx["data_freshness"]["sources"]["sleep"] is True
    assert ctx["data_freshness"]["sources"]["hydration"] is False  # none logged


# ── 14. Stale Garmin sync triggers a refresh via injected callback ──────────────

def test_stale_sync_triggers_refresh(db: Database) -> None:
    _seed(db, days=5)
    # No pull_log rows → considered stale. Inject a fake sync and assert it ran.
    calls = {"n": 0}

    def fake_sync():
        calls["n"] += 1
        return {"synced": True}

    ctx = build_coaching_context(db, refresh_if_stale=True, garmin_sync=fake_sync)
    assert calls["n"] == 1
    assert ctx["data_freshness"]["refresh"]["attempted"] is True


def test_fresh_sync_does_not_refresh(db: Database) -> None:
    _seed(db, days=5)
    db.record_pull(date.today().isoformat(), {"sleep": "ok"})  # recent pull → fresh
    calls = {"n": 0}

    def fake_sync():
        calls["n"] += 1
        return {}

    build_coaching_context(db, refresh_if_stale=True, garmin_sync=fake_sync)
    assert calls["n"] == 0


def test_refresh_failure_degrades_gracefully(db: Database) -> None:
    _seed(db, days=5)

    def broken_sync():
        raise RuntimeError("garmin auth expired")

    ctx = build_coaching_context(db, refresh_if_stale=True, garmin_sync=broken_sync)
    # Never crashes — the failure is reported, latest cached data still returned.
    assert "error" in ctx["data_freshness"]["refresh"]
    assert ctx["recovery"]["status"] in ("good", "moderate", "low", "compromised")


# ── 9. Missing hydration is unknown, never zero ─────────────────────────────────

def test_missing_hydration_is_unknown_not_zero(db: Database) -> None:
    _seed(db, days=5)  # seed logs no hydration
    ctx = build_coaching_context(db)
    assert ctx["hydration"]["today"] is None
    assert any(w["field"] == "hydration_ml" and w["status"] == "missing"
               for w in ctx["data_quality_warnings"])
    # Targets are still surfaced so the coach knows the goal.
    assert ctx["hydration"]["targets"]["baseline_ml"] == 2750


# ── 15. Data-quality warnings for suspicious workout values ─────────────────────

def test_suspicious_workout_values_are_flagged() -> None:
    warnings = detect_workout_quality_warnings([
        {"activity_id": 1, "type": "running", "duration_s": 1800, "distance_m": 0},
        {"activity_id": 2, "type": "running", "duration_s": 600, "distance_m": 9000},
        {"activity_id": 3, "type": "strength_training", "duration_s": 3000, "distance_m": None},
    ])
    fields = {(w["activity_id"], w["field"]) for w in warnings}
    assert (1, "distance_m") in fields  # zero distance on a run
    assert any(w["activity_id"] == 2 for w in warnings)  # implausible speed
    assert not any(w["activity_id"] == 3 for w in warnings)  # strength has no distance


# ── Nutrition: sodium, calorie-only, full-macro, idempotent import ───────────────

_EXPORT_SODIUM = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
  <Record type="HKQuantityTypeIdentifierDietaryEnergyConsumed" unit="Cal" value="600"
          startDate="2026-07-01 12:30:00 +0200" endDate="2026-07-01 12:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryProtein" unit="g" value="40"
          startDate="2026-07-01 12:30:00 +0200" endDate="2026-07-01 12:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryCarbohydrates" unit="g" value="70"
          startDate="2026-07-01 12:30:00 +0200" endDate="2026-07-01 12:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryFatTotal" unit="g" value="20"
          startDate="2026-07-01 12:30:00 +0200" endDate="2026-07-01 12:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryFiber" unit="g" value="8"
          startDate="2026-07-01 12:30:00 +0200" endDate="2026-07-01 12:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietarySodium" unit="mg" value="1200"
          startDate="2026-07-01 12:30:00 +0200" endDate="2026-07-01 12:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietarySodium" unit="g" value="0.3"
          startDate="2026-07-01 19:30:00 +0200" endDate="2026-07-01 19:30:00 +0200"/>
</HealthData>
"""


def test_apple_nutrition_import_includes_all_macros_and_sodium(db: Database, tmp_path) -> None:
    path = tmp_path / "export.xml"
    path.write_text(_EXPORT_SODIUM)
    import_export(path, db)
    summary = db.nutrition_summary(day="2026-07-01")[0]
    assert summary["total_calories"] == 600
    assert summary["total_protein_g"] == 40
    assert summary["total_carbs_g"] == 70
    assert summary["total_fat_g"] == 20
    assert summary["total_fiber_g"] == 8
    assert summary["total_sodium_mg"] == pytest.approx(1500.0, abs=0.5)  # 1200mg + 0.3g


def test_apple_nutrition_reimport_is_idempotent(db: Database, tmp_path) -> None:
    path = tmp_path / "export.xml"
    path.write_text(_EXPORT_SODIUM)
    import_export(path, db)
    import_export(path, db)
    summary = db.nutrition_summary(day="2026-07-01")[0]
    assert summary["meal_count"] == 1  # replaced, not duplicated
    assert summary["total_sodium_mg"] == pytest.approx(1500.0, abs=0.5)


def test_calories_only_meal_is_valid(db: Database) -> None:
    db.add_meal("black coffee snack", day="2026-07-01", calories=250)
    summary = db.nutrition_summary(day="2026-07-01")[0]
    assert summary["total_calories"] == 250
    assert summary["total_protein_g"] is None  # missing macro, not zero
    assert summary["meal_count"] == 1


def test_full_macro_meal_totals(db: Database) -> None:
    db.add_meal("chicken bowl", day="2026-07-01", calories=650, protein_g=55,
                carbs_g=60, fat_g=18, fiber_g=9, sugar_g=6, sodium_mg=800)
    summary = db.nutrition_summary(day="2026-07-01")[0]
    assert summary["total_protein_g"] == 55
    assert summary["total_sodium_mg"] == 800


def test_source_record_id_makes_meal_import_idempotent(db: Database) -> None:
    db.add_meal("Lunch", day="2026-07-01", calories=500, source="apple", source_record_id="rec-1")
    db.add_meal("Lunch", day="2026-07-01", calories=520, source="apple", source_record_id="rec-1")
    summary = db.nutrition_summary(day="2026-07-01")[0]
    assert summary["meal_count"] == 1  # same source record updated in place
    assert summary["total_calories"] == 520  # latest value wins


# ── Hydration targets configurable and persisted ────────────────────────────────

def test_hydration_targets_default_and_override(db: Database) -> None:
    assert db.hydration_targets()["baseline_ml"] == 2750
    db.set_profile(hydration_baseline_target_ml=3000, hydration_training_day_target_ml=3500)
    t = db.hydration_targets()
    assert t["baseline_ml"] == 3000
    assert t["training_day_ml"] == 3500


# ── Feature-request backlog ─────────────────────────────────────────────────────

def test_new_columns_migrate_onto_legacy_db_without_data_loss(tmp_path) -> None:
    """Opening a pre-upgrade database adds the new nullable columns/tables in
    place, preserving every existing meal and profile row (and its notes)."""
    import sqlite3

    p = str(tmp_path / "legacy.db")
    c = sqlite3.connect(p)
    c.execute(
        "CREATE TABLE meal (id INTEGER PRIMARY KEY, day TEXT, ts TEXT, name TEXT, "
        "calories INTEGER, protein_g REAL, carbs_g REAL, fat_g REAL, fiber_g REAL, "
        "sugar_g REAL, note TEXT)"
    )
    c.execute("INSERT INTO meal (day, name, calories, protein_g) "
              "VALUES ('2026-07-01','legacy lunch',500,30)")
    c.execute("CREATE TABLE profile (id INTEGER PRIMARY KEY, age INTEGER, notes TEXT, "
              "updated_at TEXT NOT NULL DEFAULT '2026-01-01')")
    c.execute("INSERT INTO profile (id, age, notes) VALUES (1, 25, 'keep me')")
    c.commit()
    c.close()

    db = Database(path=p)  # init_schema reconciles the missing columns/tables

    s = db.nutrition_summary(day="2026-07-01")[0]
    assert s["total_calories"] == 500 and s["total_protein_g"] == 30  # preserved
    assert db.get_profile()["notes"] == "keep me"  # notes never dropped

    # New provenance columns are now writable on the migrated table.
    db.add_meal("new", day="2026-07-01", calories=200, sodium_mg=400,
                source="apple", source_record_id="x1", is_estimated=True)
    assert db.nutrition_summary(day="2026-07-01")[0]["total_sodium_mg"] == 400
    # New tables exist.
    db.set_sleep_target(7.0)
    assert db.sleep_target_for() == 7.0


def test_feature_request_crud(db: Database) -> None:
    fr = db.create_feature_request("Add VO2max trend", priority="medium")
    assert fr["status"] == "requested"
    updated = db.update_feature_request(fr["id"], status="planned")
    assert updated["status"] == "planned"
    assert db.list_feature_requests(status="planned")[0]["id"] == fr["id"]
