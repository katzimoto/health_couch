"""Next-session weight recommendations from logged strength history.

Turns the last logged performance of an exercise plus the current recovery
state into a concrete prescription: which weight, whether to increase /
maintain / reduce, and why. Double-progression heuristic, documented so
tuning is deliberate:

* RPE ≤ 7 and the exercise was completed → the weight was comfortably owned:
  increase by ~2.5% rounded to a 2.5 kg plate step (minimum one step).
* RPE up to 8.5 → right zone: keep the weight, try to add a rep.
* RPE above 8.5, a pain note, or a skipped/incomplete exercise → back off 5%.
* No RPE logged → completed sets are treated like RPE ≤ 7 only when reps hit
  the plan; otherwise maintain.

Recovery gates the whole session: when the analyzer flags poor recovery (HRV
down, sleep debt, resting-HR jump, load spike) or a fresh readiness check-in
reports high soreness / low energy / poor sleep, no exercise gets an
increase — the caution reason is surfaced so the coach can explain itself.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from .exercise_metrics import (
    normalize_performance,
    parse_float,
    parse_rep_range,
)

PLATE_STEP_KG = 2.5

_INCREASE_MAX_RPE = 7.0
_MAINTAIN_MAX_RPE = 8.5
_REDUCE_FACTOR = 0.95
_INCREASE_FACTOR = 1.025


def _round_to_plate(kg: float) -> float:
    return round(kg / PLATE_STEP_KG) * PLATE_STEP_KG


def recovery_caution(report: dict[str, Any]) -> str | None:
    """Why today should not add intensity, or None when recovery looks fine.

    ``report`` is the analyzer's full report; the readiness entry only counts
    when it's from today or yesterday (stale check-ins say nothing about now).
    """
    reasons: list[str] = []
    if report.get("available"):
        recovery_keys = ("HRV", "Sleep debt", "Resting HR", "Training load spiking")
        reasons += [
            flag for flag in report.get("flags", [])
            if any(key in flag for key in recovery_keys)
        ]
    readiness = report.get("readiness") or {}
    fresh_since = (date.today() - timedelta(days=1)).isoformat()
    if (readiness.get("day") or "") >= fresh_since:
        if (parse_float(readiness.get("soreness_1_10")) or 0) >= 7:
            reasons.append("high reported soreness")
        energy = parse_float(readiness.get("energy_1_10"))
        if energy is not None and energy <= 3:
            reasons.append("low reported energy")
        sleep_quality = parse_float(readiness.get("sleep_quality_1_10"))
        if sleep_quality is not None and sleep_quality <= 3:
            reasons.append("poor reported sleep quality")
    return "; ".join(reasons) if reasons else None


def recommend_next_weight(
    last: dict[str, Any],
    caution: str | None = None,
    exercise_name: str | None = None,
) -> dict[str, Any]:
    """Prescription from one exercise-history entry (as returned by
    ``get_exercise_history``): action, weight, and the reasoning.

    Every numeric input goes through the shared normalizers, so legacy rows
    holding strings, rep ranges, or lists can't crash the math. When the last
    session's effort can't be read reliably, the weight is held rather than
    increased and ``data_quality`` says why.
    """
    perf = normalize_performance(
        last,
        endpoint="recommend_next_weights",
        exercise_name=exercise_name,
        session_id=last.get("session_id"),
    )
    quality_notes: list[str] = []
    # History rows null out fields they couldn't read and record it in
    # data_quality — carry that through so a recommendation off degraded
    # data says so (and never mistakes "unreadable" for "never logged").
    inherited_quality = last.get("data_quality") or ""
    if inherited_quality:
        quality_notes.append(inherited_quality)
    if perf.dropped_fields:
        quality_notes.append(
            "unreadable stored values in: " + ", ".join(sorted(set(perf.dropped_fields)))
        )
    unreadable = set(perf.dropped_fields)
    if inherited_quality:
        unreadable.update(
            token for token in re.split(r"[:,]\s*", inherited_quality) if token
        )

    weight = parse_float(last.get("best_set_weight_kg"))
    if weight is None:
        weight = perf.best_set_weight_kg
    if weight is None:
        weight_logged = (
            any(last.get(key) is not None for key in ("best_set_weight_kg", "weight_kg"))
            or "weight_kg" in unreadable
        )
        if weight_logged:
            reason = ("last logged weight for this exercise couldn't be read as "
                      "a number — re-log it before progressing")
        else:
            reason = ("no prior weight logged for this exercise — start "
                      "conservative and log the session")
        return {
            "action": "log_first",
            "recommended_weight_kg": None,
            "reason": reason,
            "data_quality": "; ".join(quality_notes) or None,
        }

    rpe = perf.rpe
    effort_unreadable = any(
        token in ("rpe", "reps") or token.startswith("actual_sets")
        for token in unreadable
    )
    status = last.get("status")
    incomplete = status in ("skipped", "substituted") or last.get("completed") is False
    pain = bool(last.get("pain_note"))

    if pain or incomplete:
        action, new_weight = "reduce", _round_to_plate(weight * _REDUCE_FACTOR)
        reason = "pain reported last time" if pain else f"last session was {status or 'incomplete'}"
    elif rpe is not None and rpe > _MAINTAIN_MAX_RPE:
        action, new_weight = "reduce", _round_to_plate(weight * _REDUCE_FACTOR)
        reason = f"last RPE {rpe:g} — too close to failure to progress from"
    elif rpe is not None and rpe > _INCREASE_MAX_RPE:
        action, new_weight = "maintain", weight
        reason = f"last RPE {rpe:g} — stay here and add a rep before adding load"
    elif rpe is None and effort_unreadable:
        # No usable effort signal because the stored data couldn't be read —
        # never progress off data we can't interpret.
        action, new_weight = "maintain", weight
        reason = ("last session's effort couldn't be read reliably — hold the "
                  "weight until a clean log")
    else:
        plan_low, plan_high = parse_rep_range(last.get("planned_reps"))
        if (
            rpe is None
            and plan_high is not None
            and perf.best_reps is not None
            and perf.best_reps < plan_high
        ):
            # Double progression against the planned range: the bounds gate
            # the decision but are never multiplied as a rep count.
            action, new_weight = "maintain", weight
            reason = (
                f"no RPE logged and best set reached {perf.best_reps} of a "
                f"{plan_low}-{plan_high} rep plan — top the range before adding load"
            )
        else:
            action = "increase"
            new_weight = max(
                _round_to_plate(weight * _INCREASE_FACTOR), weight + PLATE_STEP_KG
            )
            reason = (
                f"last RPE {rpe:g} — weight is owned, progress"
                if rpe is not None
                else "completed as planned with no RPE logged — small increase"
            )

    if caution and action == "increase":
        action, new_weight = "maintain", weight
        reason = f"would have increased, but recovery says hold: {caution}"

    return {
        "action": action,
        "recommended_weight_kg": new_weight,
        "last_weight_kg": weight,
        "last_rpe": rpe,
        "reason": reason,
        "data_quality": "; ".join(quality_notes) or None,
    }
