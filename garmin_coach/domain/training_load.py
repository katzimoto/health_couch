"""Training-load estimation for workouts without a Garmin-computed load.

Garmin's activity training load is EPOC-based and only present on activities
recorded by the watch (and not all of those). Apple imports, manual logs and
strength sessions arrive with ``training_load = NULL``, which the analyzer
counts as zero — so a week of real walking/running/lifting reads as
detraining. This module fills the gap with a documented heuristic, stored with
``load_source = "estimated"`` so provenance is always visible and a later
Garmin re-pull (or manual override) wins.

The scale is anchored to Garmin's: a moderate 30-minute run computes to ~75.

    load = duration_min × type multiplier × intensity factor

* type multiplier — how hard a minute of this activity typically is
* intensity factor — RPE when available (best signal for strength), else
  average HR relative to an easy-aerobic 130 bpm, else 1.0

Deliberately simple; tune the tables, not call sites.
"""

from __future__ import annotations

_TYPE_MULTIPLIER = {
    "walking": 0.6,
    "casual_walking": 0.5,
    "hiking": 1.2,
    "running": 2.4,
    "trail_running": 2.6,
    "treadmill_running": 2.4,
    "cycling": 1.6,
    "indoor_cycling": 1.6,
    "swimming": 2.0,
    "lap_swimming": 2.0,
    "strength_training": 1.5,
    "traditional_strength_training": 1.5,
    "functional_strength_training": 1.5,
    "hiit": 2.6,
    "high_intensity_interval_training": 2.6,
    "rowing": 2.0,
    "elliptical": 1.6,
    "yoga": 0.8,
    "pilates": 0.9,
}
_DEFAULT_MULTIPLIER = 1.0

# Intensity clamps keep one weird HR/RPE reading from producing silly loads.
_MIN_FACTOR, _MAX_FACTOR = 0.4, 1.8
_EASY_AEROBIC_HR = 130.0


def estimate_training_load(
    workout_type: str | None,
    duration_s: float | None,
    avg_hr: float | None = None,
    rpe: float | None = None,
) -> float | None:
    """Estimated load, or None when there's nothing to estimate from."""
    if not duration_s or duration_s <= 0:
        return None
    minutes = duration_s / 60.0
    multiplier = _TYPE_MULTIPLIER.get((workout_type or "").lower(), _DEFAULT_MULTIPLIER)
    if rpe:
        factor = rpe / 6.0  # RPE 6 ≈ steady moderate work
    elif avg_hr:
        factor = avg_hr / _EASY_AEROBIC_HR
    else:
        factor = 1.0
    factor = max(_MIN_FACTOR, min(factor, _MAX_FACTOR))
    return round(minutes * multiplier * factor, 1)
