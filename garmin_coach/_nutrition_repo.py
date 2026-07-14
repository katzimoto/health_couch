"""NutritionMixin: Meals and generic vitals: logging, edits, and the nutrition summary.

Extracted from :class:`garmin_coach.database.Database` as a mixin; the
composed ``Database`` provides shared primitives (``session``, ``_upsert``,
``_cutoff``, ``_view_rows``). Not instantiated on its own.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlmodel import select

from ._db_common import _as_day
from .models import (
    Meal,
    Vital,
)

class NutritionMixin:
    """Meals and generic vitals: logging, edits, and the nutrition summary."""

    # ── Meals (user-logged, not pulled from Garmin) ─────────────────────────────

    def add_meal(
        self,
        name: str,
        day: str | date | None = None,
        calories: int | None = None,
        protein_g: float | None = None,
        carbs_g: float | None = None,
        fat_g: float | None = None,
        fiber_g: float | None = None,
        sugar_g: float | None = None,
        sodium_mg: float | None = None,
        source: str | None = None,
        source_record_id: str | None = None,
        is_estimated: bool | None = None,
        note: str | None = None,
    ) -> int:
        """Insert a meal. Returns the new row id.

        A meal with only ``calories`` (all macros ``None``) is valid; nutrition
        totals only sum the values actually present. When ``source`` and
        ``source_record_id`` are given, an existing row from the same source
        with the same record id is updated in place instead of duplicated, so
        re-imports are idempotent.
        """
        with self.session() as s:
            existing: Meal | None = None
            if source is not None and source_record_id is not None:
                existing = s.exec(
                    select(Meal).where(
                        Meal.source == source,
                        Meal.source_record_id == source_record_id,
                    )
                ).first()
            row = existing or Meal(day=_as_day(day or date.today()), name=name)
            row.day = _as_day(day or date.today())
            row.name = name
            row.calories = calories
            row.protein_g = protein_g
            row.carbs_g = carbs_g
            row.fat_g = fat_g
            row.fiber_g = fiber_g
            row.sugar_g = sugar_g
            row.sodium_mg = sodium_mg
            row.source = source
            row.source_record_id = source_record_id
            row.is_estimated = is_estimated
            row.note = note
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.id

    def recent_meals(self, days: int = 7) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Meal).order_by(Meal.day.desc(), Meal.id.desc()).limit(days * 6)
            ).all()
        return [r.model_dump() for r in reversed(rows)]

    # ── Vitals (generic named readings: blood pressure, glucose, SpO2, etc.) ───

    def add_vital(
        self,
        metric: str,
        value: float,
        day: str | date | None = None,
        unit: str | None = None,
        note: str | None = None,
    ) -> None:
        with self.session() as s:
            s.add(
                Vital(
                    day=_as_day(day or date.today()),
                    metric=metric,
                    value=value,
                    unit=unit,
                    note=note,
                )
            )
            s.commit()

    def recent_vitals(
        self, metric: str | None = None, days: int = 30
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(Vital).order_by(Vital.day.desc(), Vital.id.desc())
            if metric:
                stmt = stmt.where(Vital.metric == metric)
            rows = s.exec(stmt.limit(days * 10)).all()
        return [r.model_dump() for r in reversed(rows)]

    # ── Meals: partial update / delete ─────────────────────────────────────────

    def update_meal(self, meal_id: int, **fields: Any) -> str | None:
        """Apply non-None fields; returns the meal's day, or None if missing."""
        with self.session() as s:
            row = s.get(Meal, meal_id)
            if row is None:
                return None
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            s.add(row)
            s.commit()
            return row.day

    def delete_meal(self, meal_id: int) -> str | None:
        with self.session() as s:
            row = s.get(Meal, meal_id)
            if row is None:
                return None
            day = row.day
            s.delete(row)
            s.commit()
            return day

    def meals_for_day(self, day: str | date) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Meal).where(Meal.day == _as_day(day)).order_by(Meal.id)
            ).all()
        return [r.model_dump() for r in rows]

    # ── Nutrition summary ──────────────────────────────────────────────────────

    def nutrition_summary(
        self, days: int = 7, day: str | date | None = None
    ) -> list[dict[str, Any]]:
        """Per-day nutrition totals vs. profile targets, newest day first.
        Meals missing macros simply contribute nothing to those totals."""
        if day is not None:
            day_list = [_as_day(day)]
            with self.session() as s:
                meal_rows = s.exec(select(Meal).where(Meal.day == day_list[0])).all()
        else:
            cutoff = self._cutoff(days)
            with self.session() as s:
                meal_rows = s.exec(
                    select(Meal).where(Meal.day >= cutoff).order_by(Meal.day, Meal.id)
                ).all()
            day_list = sorted({m.day for m in meal_rows}, reverse=True)

        profile = self.get_profile() or {}
        calorie_target = profile.get("calorie_target")
        protein_target = profile.get("protein_target_g")

        by_day: dict[str, list[Meal]] = {}
        for meal in meal_rows:
            by_day.setdefault(meal.day, []).append(meal)

        def total(meals: list[Meal], field: str) -> float | None:
            values = [getattr(m, field) for m in meals if getattr(m, field) is not None]
            return round(sum(values), 1) if values else None

        out = []
        for d in day_list:
            meals = by_day.get(d, [])
            calories = total(meals, "calories")
            protein = total(meals, "protein_g")
            out.append(
                {
                    "day": d,
                    "total_calories": calories,
                    "total_protein_g": protein,
                    "total_carbs_g": total(meals, "carbs_g"),
                    "total_fat_g": total(meals, "fat_g"),
                    "total_fiber_g": total(meals, "fiber_g"),
                    "total_sugar_g": total(meals, "sugar_g"),
                    "total_sodium_mg": total(meals, "sodium_mg"),
                    "meal_count": len(meals),
                    "calorie_target": calorie_target,
                    "protein_target_g": protein_target,
                    "calories_remaining": (
                        round(calorie_target - (calories or 0), 1)
                        if calorie_target is not None else None
                    ),
                    "protein_remaining_g": (
                        round(protein_target - (protein or 0), 1)
                        if protein_target is not None else None
                    ),
                    "meals": [m.model_dump() for m in meals],
                }
            )
        return out
