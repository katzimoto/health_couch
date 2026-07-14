"""TrainingPlanMixin: Training plans (planned workouts + adherence) and workout edits/deletes.

Extracted from :class:`garmin_coach.database.Database` as a mixin; the
composed ``Database`` provides shared primitives (``session``, ``_upsert``,
``_cutoff``, ``_view_rows``). Not instantiated on its own.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlmodel import select

from ._db_common import _as_day
from .models import (
    TrainingPlan,
    TrainingPlanEdit,
    Workout,
)

class TrainingPlanMixin:
    """Training plans (planned workouts + adherence) and workout edits/deletes."""

    # ── Training plans (planned workouts + adherence) ──────────────────────────

    def create_training_plan(self, day: str | date, **fields: Any) -> dict[str, Any]:
        exercises = fields.pop("exercises", None)
        if isinstance(exercises, (list, dict)):
            exercises = json.dumps(exercises, ensure_ascii=False)
        with self.session() as s:
            row = TrainingPlan(day=_as_day(day), exercises=exercises, **fields)
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._plan_dict(row)

    @staticmethod
    def _plan_dict(row: TrainingPlan) -> dict[str, Any]:
        out = row.model_dump()
        if out.get("exercises"):
            try:
                out["exercises"] = json.loads(out["exercises"])
            except ValueError:
                pass
        return out

    def get_training_plans(
        self, days: int = 14, status: str | None = None
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = (
                select(TrainingPlan)
                .where(TrainingPlan.day >= self._cutoff(days))
                .order_by(TrainingPlan.day, TrainingPlan.id)
            )
            if status:
                stmt = stmt.where(TrainingPlan.status == status)
            rows = s.exec(stmt).all()
        return [self._plan_dict(r) for r in rows]

    def get_training_plans_for_day(self, day: str | date) -> list[dict[str, Any]]:
        day_str = _as_day(day)
        with self.session() as s:
            rows = s.exec(
                select(TrainingPlan)
                .where(TrainingPlan.day == day_str)
                .order_by(TrainingPlan.id)
            ).all()
        return [self._plan_dict(r) for r in rows]

    def get_today_training_plans(self) -> list[dict[str, Any]]:
        return self.get_training_plans_for_day(date.today())

    def get_training_plan(self, plan_id: int) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(TrainingPlan, plan_id)
            return self._plan_dict(row) if row is not None else None

    def update_training_plan(self, plan_id: int, **fields: Any) -> dict[str, Any] | None:
        """Partial update. Only non-``None`` fields change; a changed field is
        recorded in ``training_plan_edit`` and ``updated_at`` is bumped. Raises
        ``ValueError`` for an unrecognised ``status``."""
        status = fields.get("status")
        if status is not None and status not in self._PLAN_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(self._PLAN_STATUSES)}, got {status!r}"
            )
        exercises = fields.pop("exercises", None)
        if isinstance(exercises, (list, dict)):
            fields["exercises"] = json.dumps(exercises, ensure_ascii=False)
        elif exercises is not None:
            fields["exercises"] = exercises

        with self.session() as s:
            row = s.get(TrainingPlan, plan_id)
            if row is None:
                return None
            changes: dict[str, Any] = {}
            for key, value in fields.items():
                if value is None:
                    continue
                old = getattr(row, key, None)
                if old != value:
                    changes[key] = {"old": old, "new": value}
                setattr(row, key, value)
            if changes:
                row.updated_at = datetime.now(timezone.utc)
                s.add(
                    TrainingPlanEdit(
                        plan_id=plan_id,
                        changes_json=json.dumps(changes, ensure_ascii=False, default=str),
                    )
                )
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._plan_dict(row)

    def training_plan_history(self, plan_id: int) -> list[dict[str, Any]]:
        """Audit trail of changes applied via update_training_plan, oldest first."""
        with self.session() as s:
            rows = s.exec(
                select(TrainingPlanEdit)
                .where(TrainingPlanEdit.plan_id == plan_id)
                .order_by(TrainingPlanEdit.id)
            ).all()
        out = []
        for r in rows:
            entry = r.model_dump()
            try:
                entry["changes"] = json.loads(entry.pop("changes_json"))
            except ValueError:
                entry["changes"] = {}
            out.append(entry)
        return out

    # ── Workouts: partial update / delete ──────────────────────────────────────

    def update_workout(self, activity_id: int, **fields: Any) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(Workout, activity_id)
            if row is None:
                return None
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.model_dump()

    def delete_workout(self, activity_id: int) -> str | None:
        with self.session() as s:
            row = s.get(Workout, activity_id)
            if row is None:
                return None
            day = row.day
            s.delete(row)
            s.commit()
            return day

    def workouts_for_day(self, day: str | date) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Workout)
                .where(Workout.day == _as_day(day))
                .where(Workout.duplicate_of == None)  # noqa: E711
                .order_by(Workout.activity_id)
            ).all()
        return [r.model_dump() for r in rows]
