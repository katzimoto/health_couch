"""Apple Health export importer.

Streams the ``export.xml`` inside an Apple Health ``export.zip`` (Health app →
profile picture → Export All Health Data) and writes the metrics this system
tracks into the database:

* body mass / body fat / BMI      → per-day ``weight`` row (last reading wins)
* dietary water                   → per-day ``hydration`` row (summed)
* dietary energy + macros         → one synthetic "Apple Health nutrition" meal
                                    per day (summed)
* workouts                        → ``workout`` rows with deterministic
                                    negative activity IDs
* blood pressure, glucose, SpO2,
  respiratory rate, height        → per-day ``vital`` rows (daily mean)

Steps/sleep/HR are deliberately *not* imported — Garmin owns those tables and
double-counting a second wearable would corrupt the trends.

Everything is idempotent: weights/hydration upsert, workout IDs are content-
derived, and meals/vitals tagged with :data:`NOTE_TAG` are replaced per day on
re-import. The XML is streamed (``iterparse`` + ``clear``), so multi-hundred-MB
exports parse in constant memory.
"""

from __future__ import annotations

import hashlib
import logging
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, BinaryIO
from xml.etree.ElementTree import iterparse

from sqlmodel import select

from .database import Database
from .models import Meal, Vital

log = logging.getLogger("garmin_coach.apple_health")

# Written into the note field of every meal/vital row this importer creates,
# so a re-import can replace exactly its own rows and nothing user-entered.
NOTE_TAG = "apple-health-import"

MEAL_NAME = "Apple Health nutrition (daily total)"

# ── Unit conversions (Apple's unit attribute → our storage units) ──────────────

_TO_KG = {"kg": 1.0, "lb": 0.45359237, "g": 0.001, "st": 6.35029318}
_TO_ML = {"mL": 1.0, "ml": 1.0, "L": 1000.0, "l": 1000.0, "fl_oz_us": 29.5735295625}
_TO_KCAL = {"Cal": 1.0, "kcal": 1.0, "cal": 0.001, "kJ": 1 / 4.184}
_TO_M = {"m": 1.0, "km": 1000.0, "mi": 1609.344, "yd": 0.9144, "ft": 0.3048}
_TO_S = {"s": 1.0, "sec": 1.0, "min": 60.0, "hr": 3600.0}
_TO_CM = {"cm": 1.0, "m": 100.0, "in": 2.54, "ft": 30.48}


def _convert(value: float, unit: str, table: dict[str, float]) -> float | None:
    factor = table.get(unit)
    return value * factor if factor is not None else None


def _percent(value: float) -> float:
    """HealthKit stores percentages as 0-1 fractions with unit '%'."""
    return value * 100.0 if value <= 1.0 else value


# ── Record routing ──────────────────────────────────────────────────────────────

_HK = "HKQuantityTypeIdentifier"

# Nutrition types summed into the day's synthetic meal, keyed by Meal field.
_NUTRITION = {
    f"{_HK}DietaryEnergyConsumed": "calories",
    f"{_HK}DietaryProtein": "protein_g",
    f"{_HK}DietaryCarbohydrates": "carbs_g",
    f"{_HK}DietaryFatTotal": "fat_g",
    f"{_HK}DietaryFiber": "fiber_g",
    f"{_HK}DietarySugar": "sugar_g",
}

# Quantity types stored as generic vitals, keyed by our metric name.
_VITALS = {
    f"{_HK}BloodPressureSystolic": ("blood_pressure_systolic", "mmHg"),
    f"{_HK}BloodPressureDiastolic": ("blood_pressure_diastolic", "mmHg"),
    f"{_HK}BloodGlucose": ("blood_glucose", None),  # keep the export's unit
    f"{_HK}OxygenSaturation": ("oxygen_saturation", "%"),
    f"{_HK}RespiratoryRate": ("respiratory_rate", "breaths/min"),
    f"{_HK}Height": ("height_cm", "cm"),
}


def _day_of(elem_date: str) -> str:
    """'2026-07-01 08:00:00 +0200' → '2026-07-01' (the device-local date)."""
    return elem_date[:10]


def _workout_type(hk_type: str) -> str:
    """'HKWorkoutActivityTypeTraditionalStrengthTraining' → 'traditional_strength_training'."""
    bare = hk_type.removeprefix("HKWorkoutActivityType")
    return re.sub(r"(?<!^)(?=[A-Z])", "_", bare).lower()


def _workout_id(start: str, hk_type: str) -> int:
    """Deterministic negative ID: the same workout re-imports onto itself,
    and it can never collide with Garmin's positive activity IDs."""
    digest = hashlib.md5(f"apple|{start}|{hk_type}".encode()).hexdigest()
    return -(int(digest[:12], 16) + 1)


class _Aggregates:
    """Everything collected from one pass over the XML."""

    def __init__(self) -> None:
        self.weight: dict[str, dict[str, Any]] = defaultdict(dict)  # day → fields
        self.weight_seen: dict[tuple[str, str], str] = {}  # (day, field) → startDate
        self.water_ml: dict[str, float] = defaultdict(float)
        self.nutrition: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.vitals: dict[tuple[str, str], list[float]] = defaultdict(list)
        self.vital_units: dict[str, str | None] = {}
        self.workouts: list[dict[str, Any]] = []
        self.skipped = 0

    def _weight_field(self, day: str, field: str, start: str, value: float) -> None:
        # Multiple weigh-ins a day: keep the latest by start timestamp.
        key = (day, field)
        if key not in self.weight_seen or start >= self.weight_seen[key]:
            self.weight_seen[key] = start
            self.weight[day][field] = value

    def add_record(self, attrs: dict[str, str]) -> None:
        rtype = attrs.get("type", "")
        start = attrs.get("startDate", "")
        raw = attrs.get("value")
        unit = attrs.get("unit", "")
        if not start or raw is None:
            self.skipped += 1
            return
        try:
            value = float(raw)
        except ValueError:
            self.skipped += 1
            return
        day = _day_of(start)

        if rtype == f"{_HK}BodyMass":
            kg = _convert(value, unit, _TO_KG)
            if kg is None:
                self.skipped += 1
                return
            self._weight_field(day, "weight_kg", start, round(kg, 2))
        elif rtype == f"{_HK}BodyFatPercentage":
            self._weight_field(day, "body_fat", start, round(_percent(value), 1))
        elif rtype == f"{_HK}BodyMassIndex":
            self._weight_field(day, "bmi", start, round(value, 1))
        elif rtype == f"{_HK}DietaryWater":
            ml = _convert(value, unit, _TO_ML)
            if ml is None:
                self.skipped += 1
                return
            self.water_ml[day] += ml
        elif rtype in _NUTRITION:
            field = _NUTRITION[rtype]
            if field == "calories":
                kcal = _convert(value, unit, _TO_KCAL)
                if kcal is None:
                    self.skipped += 1
                    return
                self.nutrition[day][field] += kcal
            else:
                self.nutrition[day][field] += value  # macros arrive in grams
        elif rtype in _VITALS:
            metric, fixed_unit = _VITALS[rtype]
            if metric == "oxygen_saturation":
                value = _percent(value)
            elif metric == "height_cm":
                converted = _convert(value, unit, _TO_CM)
                if converted is None:
                    self.skipped += 1
                    return
                value = converted
            self.vitals[(day, metric)].append(value)
            self.vital_units[metric] = fixed_unit if fixed_unit else (unit or None)
        # Anything else (steps, HR, sleep, mindfulness, ...) is intentionally
        # ignored — either Garmin owns it or we don't track it.

    def add_workout(self, attrs: dict[str, str]) -> None:
        start = attrs.get("startDate", "")
        hk_type = attrs.get("workoutActivityType", "")
        if not start or not hk_type:
            self.skipped += 1
            return

        def num(name: str, table: dict[str, float], unit_name: str) -> float | None:
            raw = attrs.get(name)
            if raw is None:
                return None
            try:
                return _convert(float(raw), attrs.get(unit_name, ""), table)
            except ValueError:
                return None

        wtype = _workout_type(hk_type)
        calories = num("totalEnergyBurned", _TO_KCAL, "totalEnergyBurnedUnit")
        self.workouts.append(
            {
                "activity_id": _workout_id(start, hk_type),
                "day": _day_of(start),
                "name": wtype.replace("_", " ").title(),
                "type": wtype,
                "duration_s": num("duration", _TO_S, "durationUnit"),
                "distance_m": num("totalDistance", _TO_M, "totalDistanceUnit"),
                "calories": int(calories) if calories is not None else None,
            }
        )


def _open_export(path: Path) -> BinaryIO:
    """Return a readable binary stream of export.xml, from a zip or directly."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        candidates = [
            n for n in zf.namelist()
            if n.endswith("export.xml") and "cda" not in Path(n).name.lower()
        ]
        if not candidates:
            raise FileNotFoundError(f"No export.xml found inside {path}")
        return zf.open(candidates[0])
    return open(path, "rb")


def _parse(stream: BinaryIO, since: str | None) -> _Aggregates:
    agg = _Aggregates()
    for _event, elem in iterparse(stream, events=("end",)):
        if elem.tag == "Record":
            if since is None or _day_of(elem.get("startDate", "")) >= since:
                agg.add_record(elem.attrib)
            elem.clear()
        elif elem.tag == "Workout":
            if since is None or _day_of(elem.get("startDate", "")) >= since:
                agg.add_workout(elem.attrib)
            elem.clear()
    return agg


def _replace_tagged_meals(db: Database, day: str) -> None:
    with db.session() as s:
        for row in s.exec(
            select(Meal).where(Meal.day == day, Meal.note == NOTE_TAG)
        ).all():
            s.delete(row)
        s.commit()


def _replace_tagged_vitals(db: Database, day: str, metric: str) -> None:
    with db.session() as s:
        for row in s.exec(
            select(Vital).where(
                Vital.day == day, Vital.metric == metric, Vital.note == NOTE_TAG
            )
        ).all():
            s.delete(row)
        s.commit()


def import_export(
    path: str | Path, db: Database | None = None, since: str | None = None
) -> dict[str, int]:
    """Import an Apple Health export file (zip or xml). Returns count summary."""
    db = db or Database()
    path = Path(path).expanduser()
    with _open_export(path) as stream:
        agg = _parse(stream, since)

    for day, fields in agg.weight.items():
        db.upsert_weight(day, **fields)
    for day, ml in agg.water_ml.items():
        db.upsert_hydration(day, intake_ml=int(round(ml)))
    for day, sums in agg.nutrition.items():
        _replace_tagged_meals(db, day)
        db.add_meal(
            name=MEAL_NAME,
            day=day,
            calories=int(round(sums["calories"])) if sums.get("calories") else None,
            protein_g=round(sums["protein_g"], 1) if sums.get("protein_g") else None,
            carbs_g=round(sums["carbs_g"], 1) if sums.get("carbs_g") else None,
            fat_g=round(sums["fat_g"], 1) if sums.get("fat_g") else None,
            fiber_g=round(sums["fiber_g"], 1) if sums.get("fiber_g") else None,
            sugar_g=round(sums["sugar_g"], 1) if sums.get("sugar_g") else None,
            note=NOTE_TAG,
        )
    for (day, metric), values in agg.vitals.items():
        _replace_tagged_vitals(db, day, metric)
        db.add_vital(
            metric=metric,
            value=round(mean(values), 2),
            day=day,
            unit=agg.vital_units.get(metric),
            note=NOTE_TAG,
        )
    for w in agg.workouts:
        db.upsert_workout(**w)

    counts = {
        "weight_days": len(agg.weight),
        "hydration_days": len(agg.water_ml),
        "meal_days": len(agg.nutrition),
        "vital_readings": len(agg.vitals),
        "workouts": len(agg.workouts),
        "skipped_records": agg.skipped,
    }
    log.info("Apple Health import done: %s", counts)
    return counts
