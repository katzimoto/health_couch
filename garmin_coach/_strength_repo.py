"""StrengthMixin: Strength sessions and their exercises, including history and the
Workout mirror.

Extracted from :class:`garmin_coach.database.Database` as a mixin; the
composed ``Database`` provides shared primitives (``session``, ``_upsert``,
``_cutoff``, ``_view_rows``). Not instantiated on its own.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlmodel import select

from ._db_common import _as_day, synthetic_activity_id
from .exercise_metrics import (
    log_malformed_value,
    normalize_performance,
    parse_float,
    parse_int,
)
from .models import (
    StrengthExercise,
    StrengthSession,
    Workout,
    WorkoutSourceLink,
)
from .training_load import estimate_training_load

class StrengthMixin:
    """Strength sessions and their exercises, including history and the"""

    # ── Strength sessions ──────────────────────────────────────────────────────

    def _strength_rpe(self, exercises: list[dict[str, Any]]) -> float | None:
        rpes = [parse_float(e.get("rpe")) for e in exercises]
        rpes = [r for r in rpes if r is not None]
        return sum(rpes) / len(rpes) if rpes else None

    @staticmethod
    def _prepare_exercise(ex: dict[str, Any]) -> dict[str, Any]:
        """Normalise one incoming exercise dict for storage.

        Per-set data (``actual_sets``) is kept verbatim as JSON and also
        collapsed into the aggregate columns (top weight, average reps/RPE,
        set count) so single-row history queries stay simple. Numeric columns
        are coerced through the shared parsers when they arrive as strings
        (``"3"``, ``"12.5"``); values that don't parse (a rep range, free
        text) are stored verbatim and nulled at read time by
        ``normalize_performance`` — except a list of reps, which SQLite can't
        bind and is stored as JSON for ``parse_reps`` to recover. A
        ``completed: False`` without an explicit status becomes
        status="skipped"."""
        ex = dict(ex)
        actual_sets = ex.pop("actual_sets", None)
        if actual_sets:
            ex["set_details"] = json.dumps(actual_sets, ensure_ascii=False)
            entries = [s for s in actual_sets if isinstance(s, dict)]
            ex.setdefault("sets", len(entries) or None)
            reps = [parse_int(s.get("reps")) for s in entries]
            reps = [r for r in reps if r is not None]
            if reps and ex.get("reps") is None:
                ex["reps"] = round(sum(reps) / len(reps))
            weights = [parse_float(s.get("weight_kg")) for s in entries]
            weights = [w for w in weights if w is not None]
            if weights and ex.get("weight_kg") is None:
                ex["weight_kg"] = max(weights)
            rpes = [parse_float(s.get("rpe")) for s in entries]
            rpes = [r for r in rpes if r is not None]
            if rpes and ex.get("rpe") is None:
                ex["rpe"] = round(sum(rpes) / len(rpes), 1)
        for field, parser in (
            ("sets", parse_int), ("reps", parse_int), ("planned_sets", parse_int),
            ("weight_kg", parse_float), ("planned_weight_kg", parse_float),
            ("rpe", parse_float), ("rir", parse_float), ("rest_s", parse_float),
        ):
            value = ex.get(field)
            if value is None:
                continue
            parsed = parser(value)
            if parsed is not None:
                ex[field] = parsed
            elif isinstance(value, (list, tuple)):
                ex[field] = json.dumps(value, ensure_ascii=False)
        if ex.get("status") is None and ex.get("completed") is False:
            ex["status"] = "skipped"
        return ex

    @staticmethod
    def _exercise_dict(row: StrengthExercise) -> dict[str, Any]:
        out = row.model_dump()
        detail = out.pop("set_details", None)
        if detail:
            try:
                out["actual_sets"] = json.loads(detail)
            except ValueError:
                out["actual_sets"] = None
        else:
            out["actual_sets"] = None
        return out

    def add_strength_session(
        self,
        day: str | date,
        exercises: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Create a strength session plus its exercises, mirrored into a
        Workout row (estimated training load) for history and load math."""
        prepared = [self._prepare_exercise(ex) for ex in (exercises or [])]
        day_str = _as_day(day)
        activity_id = synthetic_activity_id()
        with self.session() as s:
            session_row = StrengthSession(day=day_str, activity_id=activity_id, **fields)
            s.add(session_row)
            s.flush()
            for ex in prepared:
                s.add(StrengthExercise(session_id=session_row.id, **ex))
            s.commit()
            s.refresh(session_row)
            session_id = session_row.id

        load = estimate_training_load(
            "strength_training",
            fields.get("duration_s"),
            avg_hr=fields.get("avg_hr"),
            rpe=self._strength_rpe(prepared),
        )
        self.upsert_workout(
            activity_id,
            day_str,
            name=fields.get("session_name") or "Strength session",
            type="strength_training",
            duration_s=fields.get("duration_s"),
            calories=fields.get("calories"),
            avg_hr=fields.get("avg_hr"),
            max_hr=fields.get("max_hr"),
            training_load=load,
            source="manual",
            load_source="estimated" if load is not None else None,
        )
        return self.get_strength_session(session_id)

    def get_strength_session(self, session_id: int) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(StrengthSession, session_id)
            if row is None:
                return None
            exercises = s.exec(
                select(StrengthExercise)
                .where(StrengthExercise.session_id == session_id)
                .order_by(StrengthExercise.id)
            ).all()
            out = row.model_dump()
            out["exercises"] = [self._exercise_dict(e) for e in exercises]
            return out

    def recent_strength_sessions(self, days: int = 30) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(StrengthSession)
                .where(StrengthSession.day >= self._cutoff(days))
                .order_by(StrengthSession.day, StrengthSession.id)
            ).all()
            ids = [r.id for r in rows]
        return [self.get_strength_session(i) for i in ids]

    def update_strength_session(
        self,
        session_id: int,
        exercises: list[dict[str, Any]] | None = None,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Partial update; passing ``exercises`` replaces the exercise list."""
        with self.session() as s:
            row = s.get(StrengthSession, session_id)
            if row is None:
                return None
            for key, value in fields.items():
                if value is not None:
                    setattr(row, key, value)
            if exercises is not None:
                for old in s.exec(
                    select(StrengthExercise).where(StrengthExercise.session_id == session_id)
                ).all():
                    s.delete(old)
                for ex in exercises:
                    s.add(StrengthExercise(session_id=session_id, **self._prepare_exercise(ex)))
            s.add(row)
            s.commit()
            activity_id, day = row.activity_id, row.day

        updated = self.get_strength_session(session_id)
        if activity_id is not None:
            load = estimate_training_load(
                "strength_training",
                updated.get("duration_s"),
                avg_hr=updated.get("avg_hr"),
                rpe=self._strength_rpe(updated["exercises"]),
            )
            self._mirror_strength_to_workout(activity_id, day, updated, load)
        return updated

    def _mirror_strength_to_workout(
        self, activity_id: int, day: str, updated: dict[str, Any], load: float | None
    ) -> None:
        """Write a strength session's summary into its mirrored Workout row.

        If that row is a field-level ``merged`` canonical, the manual estimate
        must not clobber the Garmin physiology it carries — so it's written to
        the manual *source* row and the canonical is re-derived from all sources
        (Garmin load/HR win again). Otherwise it's a plain upsert as before."""
        with self.session() as s:
            target = s.get(Workout, activity_id)
            is_merged = target is not None and target.source == "merged"
            manual_source_id = activity_id
            if is_merged:
                link = s.exec(
                    select(WorkoutSourceLink)
                    .where(WorkoutSourceLink.canonical_activity_id == activity_id)
                    .where(WorkoutSourceLink.source == "manual")
                ).first()
                if link is not None:
                    manual_source_id = link.source_activity_id
        self.upsert_workout(
            manual_source_id,
            day,
            name=updated.get("session_name"),
            duration_s=updated.get("duration_s"),
            calories=updated.get("calories"),
            avg_hr=updated.get("avg_hr"),
            max_hr=updated.get("max_hr"),
            training_load=load,
            load_source="estimated" if load is not None else None,
        )
        if is_merged:
            self._refresh_canonical(activity_id)

    def delete_strength_session(self, session_id: int) -> bool:
        with self.session() as s:
            row = s.get(StrengthSession, session_id)
            if row is None:
                return False
            activity_id = row.activity_id
            merged = (
                activity_id is not None
                and (wk := s.get(Workout, activity_id)) is not None
                and wk.source == "merged"
            )
        # If the session was field-level merged, unmerge first: that restores
        # the Garmin source row (so the physiological activity survives) and
        # reattaches this session to its own manual row, which is then deleted.
        if merged:
            self.unmerge_workout_sources(activity_id)
            with self.session() as s:
                row = s.get(StrengthSession, session_id)
                activity_id = row.activity_id if row is not None else None
        with self.session() as s:
            row = s.get(StrengthSession, session_id)
            if row is None:
                return False
            for ex in s.exec(
                select(StrengthExercise).where(StrengthExercise.session_id == session_id)
            ).all():
                s.delete(ex)
            s.delete(row)
            if activity_id is not None:
                workout = s.get(Workout, activity_id)
                if workout is not None:
                    s.delete(workout)
            s.commit()
        return True

    def exercise_history(
        self, exercise_name: str, days: int = 180, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Past occurrences of one exercise (newest first) with volume and
        best-set info — the raw material for progressive-overload checks."""
        with self.session() as s:
            rows = s.exec(
                select(StrengthExercise, StrengthSession)
                .where(StrengthExercise.session_id == StrengthSession.id)
                .where(StrengthSession.day >= self._cutoff(days))
                .where(StrengthExercise.exercise_name.ilike(exercise_name))
                .order_by(StrengthSession.day.desc(), StrengthExercise.id.desc())
                .limit(limit)
            ).all()
        history = []
        for exercise, session in rows:
            detail = self._exercise_dict(exercise)
            # Legacy/malformed rows can hold strings, ranges, or JSON lists in
            # the numeric columns — all math goes through the normalizer, and
            # unusable fields come back as None instead of failing the row.
            perf = normalize_performance(
                detail,
                endpoint="get_exercise_history",
                exercise_name=exercise.exercise_name,
                session_id=session.id,
                row_id=exercise.id,
            )
            dropped = list(perf.dropped_fields)
            for planned_field, parser in (
                ("planned_sets", parse_int), ("planned_weight_kg", parse_float),
            ):
                raw = detail.get(planned_field)
                if raw is not None and parser(raw) is None:
                    dropped.append(planned_field)
                    log_malformed_value(
                        "get_exercise_history", exercise.exercise_name,
                        session.id, exercise.id, planned_field, raw,
                    )
            history.append(
                {
                    "date": session.day,
                    "session_id": session.id,
                    "session_name": session.session_name,
                    "gym": session.gym,
                    "machine": exercise.machine,
                    "planned_sets": parse_int(exercise.planned_sets),
                    "planned_reps": exercise.planned_reps,
                    "planned_weight_kg": parse_float(exercise.planned_weight_kg),
                    "sets": perf.sets,
                    "reps": round(perf.average_reps) if perf.average_reps is not None else None,
                    "weight_kg": perf.weight_kg,
                    "actual_sets": detail.get("actual_sets") or None,
                    "estimated_volume_kg": perf.volume,
                    "best_set_weight_kg": perf.best_set_weight_kg,
                    "rpe": perf.rpe,
                    "rir": perf.rir,
                    "status": exercise.status,
                    "substitute_exercise": exercise.substitute_exercise,
                    "completed": exercise.completed,
                    "pain_note": exercise.pain_note,
                    "data_quality": (
                        f"unreadable stored values in: {', '.join(dropped)}"
                        if dropped else None
                    ),
                }
            )
        return history

    def recently_trained_exercises(self, days: int = 120) -> list[str]:
        """Distinct exercise names trained in the window, most recent first."""
        with self.session() as s:
            rows = s.exec(
                select(StrengthExercise.exercise_name, StrengthSession.day)
                .where(StrengthExercise.session_id == StrengthSession.id)
                .where(StrengthSession.day >= self._cutoff(days))
                .order_by(StrengthSession.day.desc())
            ).all()
        seen: list[str] = []
        for name, _day in rows:
            if name.lower() not in {n.lower() for n in seen}:
                seen.append(name)
        return seen
