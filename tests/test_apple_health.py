"""Offline tests for the Apple Health export importer (no network)."""

from __future__ import annotations

import zipfile

import pytest
from sqlmodel import select

from garmin_coach.apple_health import MEAL_NAME, NOTE_TAG, import_export
from garmin_coach.database import Database
from garmin_coach.models import Meal, Vital, Workout

_EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HealthData locale="en_US">
  <Record type="HKQuantityTypeIdentifierBodyMass" unit="lb" value="176.37"
          startDate="2026-07-01 07:30:00 +0200" endDate="2026-07-01 07:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierBodyMass" unit="kg" value="79.6"
          startDate="2026-07-01 21:00:00 +0200" endDate="2026-07-01 21:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierBodyFatPercentage" unit="%" value="0.185"
          startDate="2026-07-01 07:30:00 +0200" endDate="2026-07-01 07:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryWater" unit="mL" value="500"
          startDate="2026-07-01 09:00:00 +0200" endDate="2026-07-01 09:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryWater" unit="L" value="1"
          startDate="2026-07-01 14:00:00 +0200" endDate="2026-07-01 14:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryEnergyConsumed" unit="Cal" value="650"
          startDate="2026-07-01 12:30:00 +0200" endDate="2026-07-01 12:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryEnergyConsumed" unit="Cal" value="400"
          startDate="2026-07-01 19:30:00 +0200" endDate="2026-07-01 19:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierDietaryProtein" unit="g" value="45"
          startDate="2026-07-01 12:30:00 +0200" endDate="2026-07-01 12:30:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" unit="mmHg" value="118"
          startDate="2026-07-01 08:00:00 +0200" endDate="2026-07-01 08:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierBloodPressureSystolic" unit="mmHg" value="122"
          startDate="2026-07-01 20:00:00 +0200" endDate="2026-07-01 20:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierOxygenSaturation" unit="%" value="0.97"
          startDate="2026-07-01 08:00:00 +0200" endDate="2026-07-01 08:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierStepCount" unit="count" value="4000"
          startDate="2026-07-01 10:00:00 +0200" endDate="2026-07-01 11:00:00 +0200"/>
  <Record type="HKQuantityTypeIdentifierBodyMass" unit="kg" value="79.2"
          startDate="2026-06-01 07:30:00 +0200" endDate="2026-06-01 07:30:00 +0200"/>
  <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="30"
           durationUnit="min" totalDistance="5" totalDistanceUnit="km"
           totalEnergyBurned="320" totalEnergyBurnedUnit="Cal"
           startDate="2026-07-01 06:00:00 +0200" endDate="2026-07-01 06:30:00 +0200"/>
</HealthData>
"""


@pytest.fixture()
def db(tmp_path) -> Database:
    return Database(path=str(tmp_path / "apple.db"))


@pytest.fixture()
def export_xml(tmp_path):
    path = tmp_path / "export.xml"
    path.write_text(_EXPORT_XML)
    return path


def test_import_maps_and_converts_units(db: Database, export_xml) -> None:
    counts = import_export(export_xml, db)
    assert counts["weight_days"] == 2
    assert counts["workouts"] == 1

    rows = {r["day"]: r for r in db.daily_summary(days=10_000)}
    day = rows["2026-07-01"]
    assert day["weight_kg"] == 79.6  # latest weigh-in of the day wins
    assert day["body_fat"] == 18.5  # 0.185 fraction → percent
    assert day["hydration_ml"] == 1500  # 500 mL + 1 L
    assert day["calories_in"] == 1050  # both meals summed into one entry
    assert day["workout_count"] == 1
    assert rows["2026-06-01"]["weight_kg"] == 79.2

    with db.session() as s:
        workout = s.exec(select(Workout)).one()
        assert workout.type == "running"
        assert workout.duration_s == 1800.0  # 30 min → s
        assert workout.distance_m == 5000.0  # 5 km → m
        assert workout.activity_id < 0  # never collides with Garmin

    vitals = {v["metric"]: v for v in db.recent_vitals(days=10_000)}
    assert vitals["blood_pressure_systolic"]["value"] == 120.0  # mean of 118/122
    assert vitals["oxygen_saturation"]["value"] == 97.0
    assert "step_count" not in vitals  # steps belong to Garmin


def test_reimport_is_idempotent(db: Database, export_xml) -> None:
    import_export(export_xml, db)
    import_export(export_xml, db)

    with db.session() as s:
        meals = s.exec(select(Meal).where(Meal.note == NOTE_TAG)).all()
        systolic = s.exec(
            select(Vital).where(Vital.metric == "blood_pressure_systolic")
        ).all()
        workouts = s.exec(select(Workout)).all()
    assert len(meals) == 1 and meals[0].name == MEAL_NAME
    assert len(systolic) == 1
    assert len(workouts) == 1  # deterministic ID upserted onto itself


def test_import_reads_zip_and_respects_since(db: Database, export_xml, tmp_path) -> None:
    zip_path = tmp_path / "export.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(export_xml, "apple_health_export/export.xml")

    counts = import_export(zip_path, db, since="2026-07-01")
    assert counts["weight_days"] == 1  # the June weigh-in is filtered out
    days = {r["day"] for r in db.daily_summary(days=10_000)}
    assert days == {"2026-07-01"}


def test_manual_meal_survives_reimport(db: Database, export_xml) -> None:
    db.add_meal("home-cooked dinner", day="2026-07-01", calories=700)
    import_export(export_xml, db)
    with db.session() as s:
        names = {m.name for m in s.exec(select(Meal)).all()}
    assert "home-cooked dinner" in names  # only NOTE_TAG rows get replaced
