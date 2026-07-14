"""ProfileMixin: User profile/goals, the effective-dated sleep target, hydration
targets, and the feature-request backlog.

Extracted from :class:`garmin_coach.database.Database` as a mixin; the
composed ``Database`` provides shared primitives (``session``, ``_upsert``,
``_cutoff``, ``_view_rows``). Not instantiated on its own.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlmodel import select

from ._db_common import _as_day
from .models import (
    FeatureRequest,
    Profile,
    SleepTargetHistory,
)

class ProfileMixin:
    """User profile/goals, the effective-dated sleep target, hydration"""

    # ── Profile / goals (single row, id=1) ─────────────────────────────────────

    def get_profile(self) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(Profile, 1)
        return row.model_dump() if row else None

    def set_profile(self, replace: bool = False, **fields: Any) -> dict[str, Any]:
        """Update profile fields. Partial by default (None leaves a field
        alone); ``replace=True`` rewrites the whole profile from ``fields``."""
        with self.session() as s:
            row = s.get(Profile, 1)
            if row is None or replace:
                if row is not None:
                    s.delete(row)
                    s.flush()
                row = Profile(id=1, **{k: v for k, v in fields.items() if v is not None})
            else:
                for key, value in fields.items():
                    if value is not None:
                        setattr(row, key, value)
                row.updated_at = datetime.now()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.model_dump()

    # ── Configurable sleep target (effective-dated) ────────────────────────────

    # The user's baseline is 7.0h, NOT a hard-coded 8. Everything that reasons
    # about sleep debt resolves the target through ``sleep_target_for`` so a
    # change is honoured going forward without rewriting history.
    DEFAULT_SLEEP_TARGET_HOURS = 7.0
    DEFAULT_SLEEP_MIN_RECOVERY_HOURS = 6.0

    def set_sleep_target(
        self,
        target_hours: float,
        effective_from: str | date | None = None,
        minimum_recovery_hours: float | None = None,
        preferred_min_hours: float | None = None,
        preferred_max_hours: float | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record a sleep-target change effective from ``effective_from``
        (default today). Appends a ``SleepTargetHistory`` row so past sleep-debt
        numbers stay reproducible, and mirrors the current value onto the
        profile. Re-setting the same effective date overwrites that row rather
        than stacking duplicates."""
        eff = _as_day(effective_from or date.today())
        with self.session() as s:
            existing = s.exec(
                select(SleepTargetHistory).where(
                    SleepTargetHistory.effective_from == eff
                )
            ).first()
            row = existing or SleepTargetHistory(effective_from=eff, target_hours=target_hours)
            row.target_hours = target_hours
            row.minimum_recovery_hours = minimum_recovery_hours
            row.note = note
            s.add(row)
            s.commit()
        # Mirror onto the profile for quick reads / display.
        self.set_profile(
            sleep_target_hours=target_hours,
            sleep_minimum_recovery_hours=minimum_recovery_hours,
            sleep_preferred_min_hours=preferred_min_hours,
            sleep_preferred_max_hours=preferred_max_hours,
            sleep_target_effective_from=eff,
        )
        return {"effective_from": eff, "target_hours": target_hours}

    def _sleep_target_history(self) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(SleepTargetHistory).order_by(SleepTargetHistory.effective_from)
            ).all()
        return [r.model_dump() for r in rows]

    def sleep_target_for(self, day: str | date | None = None) -> float:
        """The sleep target (hours) effective on ``day``.

        Resolution order: the latest ``SleepTargetHistory`` row whose
        ``effective_from`` is on-or-before ``day``; else the profile's current
        ``sleep_target_hours``; else the 7.0h default. Never returns the legacy
        8-hour assumption."""
        target_day = _as_day(day or date.today())
        applicable = [
            h for h in self._sleep_target_history()
            if (h.get("effective_from") or "") <= target_day
        ]
        if applicable:
            return float(applicable[-1]["target_hours"])
        profile = self.get_profile() or {}
        val = profile.get("sleep_target_hours")
        if val is not None:
            return float(val)
        return self.DEFAULT_SLEEP_TARGET_HOURS

    def sleep_minimum_recovery_hours(self) -> float:
        profile = self.get_profile() or {}
        val = profile.get("sleep_minimum_recovery_hours")
        return float(val) if val is not None else self.DEFAULT_SLEEP_MIN_RECOVERY_HOURS

    # ── Hydration targets (persistent, source of truth for goals) ──────────────

    DEFAULT_HYDRATION_BASELINE_ML = 2750
    DEFAULT_HYDRATION_TRAINING_ML = 3250
    DEFAULT_HYDRATION_HOT_ML = 3250

    def hydration_targets(self) -> dict[str, Any]:
        """Configured hydration goals, falling back to the documented defaults.
        Never invents an intake — only the target thresholds live here."""
        profile = self.get_profile() or {}
        return {
            "baseline_ml": profile.get("hydration_baseline_target_ml")
            or self.DEFAULT_HYDRATION_BASELINE_ML,
            "training_day_ml": profile.get("hydration_training_day_target_ml")
            or self.DEFAULT_HYDRATION_TRAINING_ML,
            "hot_day_ml": profile.get("hydration_hot_day_target_ml")
            or self.DEFAULT_HYDRATION_HOT_ML,
            "medical_limit_note": profile.get("hydration_medical_limit_note"),
        }

    # ── Feature-request backlog ────────────────────────────────────────────────

    _FEATURE_STATUSES = {
        "requested", "planned", "in_progress", "blocked", "implemented", "rejected",
    }

    def create_feature_request(self, title: str, **fields: Any) -> dict[str, Any]:
        with self.session() as s:
            row = FeatureRequest(
                title=title,
                **{k: v for k, v in fields.items() if v is not None},
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.model_dump()

    def list_feature_requests(self, status: str | None = None) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = select(FeatureRequest).order_by(FeatureRequest.id.desc())
            if status:
                stmt = stmt.where(FeatureRequest.status == status)
            rows = s.exec(stmt).all()
        return [r.model_dump() for r in rows]

    def update_feature_request(self, request_id: int, **fields: Any) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.get(FeatureRequest, request_id)
            if row is None:
                return None
            for key, value in fields.items():
                if value is not None and hasattr(row, key):
                    setattr(row, key, value)
            row.updated_at = datetime.now()
            s.add(row)
            s.commit()
            s.refresh(row)
            return row.model_dump()
