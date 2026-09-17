"""Generic per-sport progression analytics — the shared aggregation contract.

``get_activity_progress(type, days)`` answers "how is my running/swimming/
cycling going?" without the caller reconstructing it from raw workout rows.
This module owns the *shared* half of that: windowing, calendar-week
bucketing, alias normalization, weighted aggregation, trend slopes and the
response schema. Sport specialists (swimming, strength) reuse these primitives
so their totals can never disagree with the generic view.

Design rules, all of which the tests pin:

* **Modalities stay distinct.** Aliases only fold spellings of the *same*
  thing together (``"Running"`` → ``running``). Pool and open-water swimming,
  indoor and outdoor cycling, treadmill and road running are materially
  different and are never pooled by default; ``related_types`` reports the
  siblings a caller could ask for separately.
* **Aggregate pace is weighted, never an average of per-session paces.** It is
  derived from total distance over total moving time, and the response names
  the formula and the time basis it used.
* **Missing is not zero.** Every metric carries an availability marker and an
  explicit reason when unavailable; a zero or absent baseline yields
  ``percent_change: None``, never a division by zero or an invented number.
* **Canonical workouts count once.** Reads go through
  ``Database.recent_workouts`` (which excludes ``duplicate_of`` rows), and
  every session keeps its source ``activity_id`` for traceability.
* **Calendar boundaries are explicit and in the user's timezone**, so "this
  week" means the same thing to the report and to the user's watch.

The pure functions (everything above :func:`build_activity_progress`) take
plain dicts and no clock, so they are unit-testable in isolation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

# ── Activity type normalization ────────────────────────────────────────────────
# Garmin's typeKey is the canonical spelling we store. The alias table only
# maps *synonyms of the same modality* onto one key; it deliberately does NOT
# collapse indoor/outdoor or pool/open-water, which differ in pace, effort and
# comparability. Unknown types pass through normalized (lowercase, underscored)
# rather than being dropped.
_ALIASES: dict[str, str] = {
    "run": "running",
    "runnning": "running",
    "road_running": "running",
    "street_running": "running",
    "treadmill": "treadmill_running",
    "indoor_running": "treadmill_running",
    "trail_run": "trail_running",
    "walk": "walking",
    "casual_walking": "walking",
    "indoor_walking": "walking",
    "hike": "hiking",
    "bike": "cycling",
    "biking": "cycling",
    "road_biking": "cycling",
    "cycling_road": "cycling",
    "indoor_biking": "indoor_cycling",
    "virtual_ride": "indoor_cycling",
    "swim": "lap_swimming",
    "swimming": "lap_swimming",
    "pool_swim": "lap_swimming",
    "pool_swimming": "lap_swimming",
    "open_water": "open_water_swimming",
    "open_water_swim": "open_water_swimming",
    "strength": "strength_training",
    "traditional_strength_training": "strength_training",
    "functional_strength_training": "strength_training",
    "weight_training": "strength_training",
    "indoor_rowing": "rowing",
    "rower": "rowing",
    "elliptical_training": "elliptical",
}

# Sibling modalities: reported as ``related_types`` so a caller knows what was
# deliberately *not* merged into the requested type.
_SIBLINGS: dict[str, tuple[str, ...]] = {
    "running": ("treadmill_running", "trail_running"),
    "treadmill_running": ("running", "trail_running"),
    "trail_running": ("running", "treadmill_running"),
    "cycling": ("indoor_cycling",),
    "indoor_cycling": ("cycling",),
    "lap_swimming": ("open_water_swimming",),
    "open_water_swimming": ("lap_swimming",),
}

# Which family a type belongs to — drives which metrics are even meaningful.
_FAMILIES: dict[str, str] = {
    "running": "run",
    "treadmill_running": "run",
    "trail_running": "run",
    "walking": "walk",
    "hiking": "walk",
    "cycling": "bike",
    "indoor_cycling": "bike",
    "lap_swimming": "swim",
    "open_water_swimming": "swim",
    "rowing": "row",
    "elliptical": "machine",
    "strength_training": "strength",
    "yoga": "mobility",
    "pilates": "mobility",
    "hiit": "conditioning",
    "high_intensity_interval_training": "conditioning",
}

# Per family: which headline metrics are supported, and in what unit.
# ``pace`` means "time per unit distance" (lower is better); ``speed`` means
# "distance per unit time" (higher is better). A family with neither reports
# an explicit reason instead of a fabricated number.
_METRIC_PROFILE: dict[str, dict[str, Any]] = {
    "run": {"distance": True, "pace": "min_per_km", "speed": "kph"},
    "walk": {"distance": True, "pace": "min_per_km", "speed": "kph"},
    "bike": {"distance": True, "pace": None, "speed": "kph"},
    "swim": {"distance": True, "pace": "min_per_100m", "speed": "kph"},
    "row": {"distance": True, "pace": "min_per_500m", "speed": "kph"},
    "machine": {"distance": True, "pace": None, "speed": "kph"},
    "strength": {"distance": False, "pace": None, "speed": None},
    "mobility": {"distance": False, "pace": None, "speed": None},
    "conditioning": {"distance": False, "pace": None, "speed": None},
    "other": {"distance": True, "pace": None, "speed": "kph"},
}

# Distance covered by one "pace unit", for each supported pace unit.
_PACE_UNIT_METERS = {
    "min_per_km": 1000.0,
    "min_per_100m": 100.0,
    "min_per_500m": 500.0,
}

# A session shorter than this contributes to volume but not to pace/speed
# aggregates: a 40 m warm-up length or a 30-second mis-start would otherwise
# swing a weighted pace.
MIN_PACE_DISTANCE_M = 200.0

# Delegation: families whose *real* progression lives in a specialist service.
_DELEGATES = {
    "strength": "get_strength_progress",
    "swim": "get_swimming_progress",
}


def normalize_activity_type(raw: str | None) -> str | None:
    """Canonical type key for ``raw``, or None when there is nothing to read.

    Lowercases and underscores the input, then folds known synonyms. Modality
    distinctions (indoor/outdoor, pool/open-water) are preserved on purpose.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in text:
        text = text.replace("__", "_")
    if not text:
        return None
    return _ALIASES.get(text, text)


def activity_family(activity_type: str | None) -> str:
    """The metric family a type belongs to (``run``/``swim``/``strength``/…).

    Unknown types fall back to substring matching on the raw key before
    ``other``, so a device-specific label such as ``"obstacle_run"`` still
    reports distance and speed rather than nothing.
    """
    canonical = normalize_activity_type(activity_type)
    if canonical is None:
        return "other"
    if canonical in _FAMILIES:
        return _FAMILIES[canonical]
    for token, family in (
        ("swim", "swim"), ("run", "run"), ("walk", "walk"), ("hik", "walk"),
        ("cycl", "bike"), ("bike", "bike"), ("row", "row"), ("strength", "strength"),
    ):
        if token in canonical:
            return family
    return "other"


def metric_profile(activity_type: str | None) -> dict[str, Any]:
    """Which headline metrics are meaningful for this type, with their units."""
    family = activity_family(activity_type)
    profile = dict(_METRIC_PROFILE.get(family, _METRIC_PROFILE["other"]))
    profile["family"] = family
    profile["delegates_to"] = _DELEGATES.get(family)
    return profile


def related_types(activity_type: str | None) -> list[str]:
    """Sibling modalities deliberately kept separate from this type."""
    canonical = normalize_activity_type(activity_type)
    return list(_SIBLINGS.get(canonical or "", ()))


def matches_type(workout_type: str | None, requested: str | None) -> bool:
    """Whether a stored workout type belongs to the requested type.

    Exact (normalized) match only — siblings are reported, never absorbed.
    """
    return (
        normalize_activity_type(workout_type) is not None
        and normalize_activity_type(workout_type) == normalize_activity_type(requested)
    )


# ── Numbers, windows and weeks ────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    """A finite float, or None. Never raises, never guesses."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def today_in(timezone_name: str | None) -> date:
    """Today's calendar date in the configured user timezone.

    Windows are cut on the user's calendar, not UTC's: at 01:00 in Jerusalem
    "the last 7 days" must not silently mean yesterday's week.
    """
    if not timezone_name:
        return date.today()
    try:
        return datetime.now(ZoneInfo(timezone_name)).date()
    except Exception:  # noqa: BLE001 — an unknown TZ must not break analytics
        return date.today()


def window_bounds(days: int, end: date) -> tuple[str, str]:
    """``(start_iso, end_iso)`` for a ``days``-long window ending on ``end``
    inclusive."""
    days = max(1, int(days))
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()


def week_buckets(start: str, end: str) -> list[dict[str, Any]]:
    """ISO-week buckets (Monday-start) spanning ``[start, end]``.

    Each bucket names its own boundaries and whether it is *partial* — clipped
    by the window edge — so a half-week at either end can't read as a drop in
    weekly volume.
    """
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    buckets: list[dict[str, Any]] = []
    cursor = start_d - timedelta(days=start_d.weekday())
    while cursor <= end_d:
        week_end = cursor + timedelta(days=6)
        clipped_start = max(cursor, start_d)
        clipped_end = min(week_end, end_d)
        buckets.append({
            "week_start": clipped_start.isoformat(),
            "week_end": clipped_end.isoformat(),
            "iso_week": f"{cursor.isocalendar().year}-W{cursor.isocalendar().week:02d}",
            "partial": clipped_start != cursor or clipped_end != week_end,
            "days_covered": (clipped_end - clipped_start).days + 1,
        })
        cursor = week_end + timedelta(days=1)
    return buckets


def percent_change(baseline: float | None, current: float | None) -> float | None:
    """Percentage change from ``baseline`` to ``current``, or None.

    None (not 0, not infinity) when either side is missing or the baseline is
    zero — "improved by ∞%" is never a truthful statement about training.
    """
    b, c = _num(baseline), _num(current)
    if b is None or c is None or b == 0:
        return None
    return round((c - b) / abs(b) * 100.0, 1)


def linear_slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of ``y`` over ``x``, or None below two distinct x.

    Used for weekly trends (x = week index, y = the weekly value), so the unit
    is "metric units per week" — which the response states explicitly.
    """
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    n = len(points)
    if n < 2 or len(set(xs)) < 2:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def pace_seconds(distance_m: float | None, seconds: float | None, unit: str) -> float | None:
    """Seconds per pace unit (per km / per 100 m / per 500 m), or None.

    Weighted by construction: callers pass *totals*, so this is total time over
    total distance — never the mean of per-session paces, which over-weights
    short sessions.
    """
    d, t = _num(distance_m), _num(seconds)
    unit_m = _PACE_UNIT_METERS.get(unit)
    if not d or not t or d <= 0 or t <= 0 or unit_m is None:
        return None
    return round(t / (d / unit_m), 1)


def format_pace(seconds_per_unit: float | None) -> str | None:
    """``"5:42"`` from 342.0 seconds — display only; math uses the number."""
    if seconds_per_unit is None:
        return None
    total = int(round(seconds_per_unit))
    return f"{total // 60}:{total % 60:02d}"


# ── Session aggregation (pure) ────────────────────────────────────────────────


def summarize_sessions(
    sessions: list[dict[str, Any]],
    activity_type: str | None,
    *,
    window_days: int,
    excluded_activity_ids: Iterable[int] = (),
) -> dict[str, Any]:
    """Aggregate one window of canonical sessions into the shared schema.

    ``sessions`` are workout rows (already filtered to the type and window and
    already free of duplicates). Rows whose ``activity_id`` is in
    ``excluded_activity_ids`` still count for session frequency — they happened
    — but are kept out of distance/pace aggregates, and the exclusion is
    reported rather than silently applied.

    ``time_basis`` says which clock the pace used: ``active_duration`` when the
    rows carry a separately recorded moving time (swim detail ingestion adds
    it), else ``recorded_duration``.
    """
    excluded = set(excluded_activity_ids)
    profile = metric_profile(activity_type)
    counted = [s for s in sessions if s.get("activity_id") not in excluded]

    total_duration = sum(_num(s.get("duration_s")) or 0.0 for s in counted)
    active_values = [_num(s.get("active_duration_s")) for s in counted]
    has_active = any(v is not None for v in active_values)
    total_active = sum(
        (_num(s.get("active_duration_s")) if _num(s.get("active_duration_s")) is not None
         else _num(s.get("duration_s")) or 0.0)
        for s in counted
    ) if has_active else total_duration

    distance_rows = [s for s in counted if (_num(s.get("distance_m")) or 0) > 0]
    total_distance = sum(_num(s.get("distance_m")) or 0.0 for s in distance_rows)
    total_load = sum(_num(s.get("training_load")) or 0.0 for s in counted)
    load_sources = sorted({s.get("load_source") for s in counted if s.get("load_source")})

    # Pace/speed only over sessions long enough to be comparable.
    pace_rows = [
        s for s in distance_rows
        if (_num(s.get("distance_m")) or 0) >= MIN_PACE_DISTANCE_M
        and (_num(s.get("active_duration_s")) or _num(s.get("duration_s")) or 0) > 0
    ]
    pace_distance = sum(_num(s.get("distance_m")) or 0.0 for s in pace_rows)
    pace_time = sum(
        (_num(s.get("active_duration_s")) or _num(s.get("duration_s")) or 0.0)
        for s in pace_rows
    )

    hr_pairs = [
        (_num(s.get("avg_hr")), _num(s.get("duration_s")) or 0.0)
        for s in counted if _num(s.get("avg_hr")) is not None
    ]
    hr_weight = sum(w for _hr, w in hr_pairs)
    avg_hr = (
        round(sum(hr * w for hr, w in hr_pairs) / hr_weight, 1)
        if hr_pairs and hr_weight > 0 else
        (round(sum(hr for hr, _w in hr_pairs) / len(hr_pairs), 1) if hr_pairs else None)
    )

    weeks = max(window_days / 7.0, 1 / 7.0)
    count = len(counted)

    summary: dict[str, Any] = {
        "workout_count": count,
        "sessions_per_week": round(count / weeks, 2),
        "total_duration_s": round(total_duration, 1) if count else 0.0,
        "weekly_duration_s": round(total_duration / weeks, 1) if count else 0.0,
        "avg_session_duration_s": round(total_duration / count, 1) if count else None,
        "total_distance_m": round(total_distance, 1) if distance_rows else None,
        "weekly_distance_m": round(total_distance / weeks, 1) if distance_rows else None,
        "avg_session_distance_m": (
            round(total_distance / len(distance_rows), 1) if distance_rows else None
        ),
        "total_training_load": round(total_load, 1) if count else 0.0,
        "weekly_training_load": round(total_load / weeks, 1) if count else 0.0,
        "load_sources": load_sources,
        "avg_hr": avg_hr,
        "avg_hr_method": "duration-weighted mean of per-session average HR" if avg_hr else None,
        "hr_coverage": {
            "sessions_with_hr": len(hr_pairs),
            "sessions_total": count,
        },
        "time_basis": "active_duration" if has_active else "recorded_duration",
        "excluded_activity_ids": sorted(excluded & {
            s.get("activity_id") for s in sessions if s.get("activity_id") is not None
        }),
    }

    pace_unit = profile.get("pace")
    if not profile.get("distance"):
        summary["distance_available"] = False
        summary["distance_unavailable_reason"] = (
            f"{profile['family']} activities do not record a meaningful distance"
        )
        summary["total_distance_m"] = None
        summary["weekly_distance_m"] = None
        summary["avg_session_distance_m"] = None
    else:
        summary["distance_available"] = bool(distance_rows)
        if not distance_rows:
            summary["distance_unavailable_reason"] = (
                "no session in this window recorded a distance"
            )

    if pace_unit is None:
        summary["pace"] = None
        summary["pace_unavailable_reason"] = (
            f"pace is not a supported metric for {profile['family']} activities"
            if not profile.get("speed")
            else f"{profile['family']} progression is reported as speed, not pace"
        )
    elif not pace_rows:
        summary["pace"] = None
        summary["pace_unavailable_reason"] = (
            f"no session covered the {MIN_PACE_DISTANCE_M:.0f} m minimum distance "
            "with a usable duration"
        )
    else:
        seconds = pace_seconds(pace_distance, pace_time, pace_unit)
        summary["pace"] = {
            "unit": pace_unit,
            "seconds_per_unit": seconds,
            "display": format_pace(seconds),
            "direction": "lower_is_better",
            "method": (
                f"total distance ÷ total {summary['time_basis']} over "
                f"{len(pace_rows)} session(s) ≥ {MIN_PACE_DISTANCE_M:.0f} m"
            ),
            "sample_count": len(pace_rows),
            "distance_m": round(pace_distance, 1),
            "duration_s": round(pace_time, 1),
        }

    speed_unit = profile.get("speed")
    if speed_unit and pace_rows and pace_time > 0:
        summary["speed"] = {
            "unit": speed_unit,
            "value": round(pace_distance / pace_time * 3.6, 2),
            "direction": "higher_is_better",
            "method": f"total distance ÷ total {summary['time_basis']}",
            "sample_count": len(pace_rows),
        }
    else:
        summary["speed"] = None

    summary["longest_session"] = _longest(counted)
    return summary


def _longest(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The longest session by distance, falling back to duration."""
    if not sessions:
        return None
    by_distance = [s for s in sessions if (_num(s.get("distance_m")) or 0) > 0]
    pool = by_distance or sessions
    key = "distance_m" if by_distance else "duration_s"
    best = max(pool, key=lambda s: _num(s.get(key)) or 0.0)
    return {
        "activity_id": best.get("activity_id"),
        "day": best.get("day"),
        "metric": key,
        "distance_m": _num(best.get("distance_m")),
        "duration_s": _num(best.get("duration_s")),
    }


def observed_records(sessions: list[dict[str, Any]], activity_type: str | None) -> dict[str, Any]:
    """Session-level *observed* records — explicitly not continuous-effort PRs.

    The fastest whole session at or above the comparability threshold is an
    observation about that session, not a proof that the same pace could be
    held for a standard race distance. Continuous PRs need contiguous
    length/split data, which the swimming service computes where it exists.
    """
    profile = metric_profile(activity_type)
    pace_unit = profile.get("pace")
    out: dict[str, Any] = {
        "basis": "whole-session averages over canonical workouts",
        "caveat": (
            "session-level observations, not continuous-effort personal records; "
            "a continuous PR requires contiguous split data"
        ),
        "longest_distance_m": None,
        "longest_duration_s": None,
        "best_session_pace": None,
    }
    if not sessions:
        out["available"] = False
        out["unavailable_reason"] = "no sessions of this type in the window"
        return out
    out["available"] = True

    with_distance = [s for s in sessions if (_num(s.get("distance_m")) or 0) > 0]
    if with_distance:
        best = max(with_distance, key=lambda s: _num(s.get("distance_m")) or 0.0)
        out["longest_distance_m"] = {
            "value": _num(best.get("distance_m")),
            "activity_id": best.get("activity_id"),
            "day": best.get("day"),
        }
    with_duration = [s for s in sessions if (_num(s.get("duration_s")) or 0) > 0]
    if with_duration:
        best = max(with_duration, key=lambda s: _num(s.get("duration_s")) or 0.0)
        out["longest_duration_s"] = {
            "value": _num(best.get("duration_s")),
            "activity_id": best.get("activity_id"),
            "day": best.get("day"),
        }

    if pace_unit:
        candidates = []
        for s in sessions:
            dist = _num(s.get("distance_m")) or 0
            time_s = _num(s.get("active_duration_s")) or _num(s.get("duration_s")) or 0
            if dist >= MIN_PACE_DISTANCE_M and time_s > 0:
                candidates.append((pace_seconds(dist, time_s, pace_unit), s))
        candidates = [(p, s) for p, s in candidates if p is not None]
        if candidates:
            pace, session = min(candidates, key=lambda pair: pair[0])
            out["best_session_pace"] = {
                "unit": pace_unit,
                "seconds_per_unit": pace,
                "display": format_pace(pace),
                "activity_id": session.get("activity_id"),
                "day": session.get("day"),
                "distance_m": _num(session.get("distance_m")),
                "avg_hr": _num(session.get("avg_hr")),
                "note": (
                    "whole-session average pace; compare with avg_hr before "
                    "reading it as a fitness gain"
                ),
            }
        else:
            out["best_session_pace_unavailable_reason"] = (
                f"no session reached the {MIN_PACE_DISTANCE_M:.0f} m comparability minimum"
            )
    return out


def compare(
    current: dict[str, Any], baseline: dict[str, Any], activity_type: str | None
) -> dict[str, Any]:
    """Absolute + percentage change of the headline metrics against baseline.

    Pace is reported with ``direction`` and an explicit ``improved`` flag
    because *lower* pace time is better — a naive "-4%" would otherwise read as
    a regression.
    """
    out: dict[str, Any] = {}
    for key in (
        "sessions_per_week", "weekly_duration_s", "weekly_distance_m",
        "weekly_training_load", "avg_session_duration_s", "avg_hr",
    ):
        cur, base = _num(current.get(key)), _num(baseline.get(key))
        out[key] = {
            "current": cur,
            "baseline": base,
            "absolute_change": round(cur - base, 2) if cur is not None and base is not None else None,
            "percent_change": percent_change(base, cur),
            "direction": "higher_is_more_volume",
        }
        if base is None:
            out[key]["unavailable_reason"] = "no comparable baseline data"

    cur_pace = (current.get("pace") or {}).get("seconds_per_unit")
    base_pace = (baseline.get("pace") or {}).get("seconds_per_unit")
    if cur_pace is not None and base_pace is not None:
        delta = round(cur_pace - base_pace, 1)
        out["pace"] = {
            "unit": (current.get("pace") or {}).get("unit"),
            "current_seconds_per_unit": cur_pace,
            "baseline_seconds_per_unit": base_pace,
            "absolute_change_s": delta,
            "percent_change": percent_change(base_pace, cur_pace),
            "direction": "lower_is_better",
            "improved": delta < 0,
            "interpretation": (
                "faster than baseline" if delta < 0
                else "slower than baseline" if delta > 0 else "unchanged"
            ),
            "caveat": (
                "compare alongside avg_hr and session structure — a faster "
                "average at a higher heart rate is a harder effort, not "
                "evidence of improved aerobic fitness"
            ),
        }
    else:
        out["pace"] = None
        out["pace_unavailable_reason"] = (
            "pace unavailable in one or both windows"
            if metric_profile(activity_type).get("pace")
            else "pace is not a supported metric for this activity type"
        )
    return out


def weekly_series(
    sessions: list[dict[str, Any]], start: str, end: str
) -> list[dict[str, Any]]:
    """Per-calendar-week volume, one entry per bucket in the window."""
    series: list[dict[str, Any]] = []
    for bucket in week_buckets(start, end):
        in_week = [
            s for s in sessions
            if bucket["week_start"] <= (s.get("day") or "") <= bucket["week_end"]
        ]
        series.append({
            **bucket,
            "workout_count": len(in_week),
            "duration_s": round(sum(_num(s.get("duration_s")) or 0.0 for s in in_week), 1),
            "distance_m": round(sum(_num(s.get("distance_m")) or 0.0 for s in in_week), 1),
            "training_load": round(sum(_num(s.get("training_load")) or 0.0 for s in in_week), 1),
        })
    return series


def weekly_trends(series: list[dict[str, Any]]) -> dict[str, Any]:
    """Least-squares slope per *complete* week for each weekly metric.

    Partial weeks are excluded from the fit (they measure fewer days, not less
    training) and the exclusion is reported with the sample count.
    """
    full = [w for w in series if not w["partial"]]
    out: dict[str, Any] = {
        "sample_weeks": len(full),
        "excluded_partial_weeks": len(series) - len(full),
        "method": "least-squares slope over complete calendar weeks",
    }
    if len(full) < 2:
        out["available"] = False
        out["unavailable_reason"] = (
            "fewer than two complete calendar weeks of history in the window"
        )
        return out
    out["available"] = True
    for key, unit in (
        ("distance_m", "metres per week"),
        ("duration_s", "seconds per week"),
        ("training_load", "load units per week"),
        ("workout_count", "sessions per week"),
    ):
        slope = linear_slope([(float(i), float(w[key])) for i, w in enumerate(full)])
        out[key] = {
            "slope_per_week": round(slope, 2) if slope is not None else None,
            "unit": unit,
            "direction": (
                "increasing" if slope is not None and slope > 0
                else "decreasing" if slope is not None and slope < 0
                else "flat" if slope is not None else None
            ),
        }
    return out


# ── The service ───────────────────────────────────────────────────────────────

# Hard bound on how much history a single call can scan, so a caller can't ask
# for a decade and make the read unbounded.
MAX_WINDOW_DAYS = 730
DEFAULT_SESSION_LIMIT = 25


def coverage_report(db, start: str, end: str) -> dict[str, Any]:
    """Which days in the window actually have a recorded Garmin sync.

    A day with no sync is *unknown*, not a rest day — the distinction the
    issues insist on. ``synced_days`` comes from ``pull_log``, the same source
    the scheduler's gap healing uses.
    """
    total = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    try:
        pulled = db.pulled_days(start, end)
    except Exception:  # noqa: BLE001 — coverage is context, never the payload
        pulled = set()
    missing = [
        (date.fromisoformat(start) + timedelta(days=i)).isoformat()
        for i in range(total)
        if (date.fromisoformat(start) + timedelta(days=i)).isoformat() not in pulled
    ]
    return {
        "window_days": total,
        "synced_days": total - len(missing),
        "unsynced_days": len(missing),
        "coverage_ratio": round((total - len(missing)) / total, 3) if total else None,
        "unsynced_day_list": missing[:30],
        "note": (
            "days without a recorded sync are unknown, not confirmed rest days"
        ),
    }


def build_activity_progress(
    db,
    activity_type: str,
    days: int = 90,
    *,
    baseline_days: int | None = None,
    timezone_name: str | None = None,
    include_sessions: bool = False,
    session_limit: int = DEFAULT_SESSION_LIMIT,
) -> dict[str, Any]:
    """Full progression report for one activity type.

    The analysis window is the last ``days`` calendar days (user timezone,
    today inclusive); the baseline is the equally long window immediately
    before it, so "vs baseline" always compares like durations. Everything is
    computed from canonical workouts — merged/duplicate rows are excluded by
    :meth:`Database.recent_workouts`, so a session recorded by two sources
    counts once.
    """
    from .config import settings
    from .coaching_context import detect_workout_quality_warnings

    tz = timezone_name or settings.timezone
    days = max(1, min(int(days), MAX_WINDOW_DAYS))
    baseline_days = max(1, min(int(baseline_days or days), MAX_WINDOW_DAYS))
    end = today_in(tz)
    start_iso, end_iso = window_bounds(days, end)
    base_end = date.fromisoformat(start_iso) - timedelta(days=1)
    base_start_iso, base_end_iso = window_bounds(baseline_days, base_end)

    canonical = normalize_activity_type(activity_type)
    profile = metric_profile(canonical)

    # One scan covers both windows.
    span_days = (end - date.fromisoformat(base_start_iso)).days + 1
    all_rows = db.recent_workouts(days=min(span_days, MAX_WINDOW_DAYS))
    typed = [w for w in all_rows if matches_type(w.get("type"), canonical)]
    current_rows = [w for w in typed if start_iso <= (w.get("day") or "") <= end_iso]
    baseline_rows = [
        w for w in typed if base_start_iso <= (w.get("day") or "") <= base_end_iso
    ]

    # Data-quality exclusions reuse the existing detector rather than adding a
    # competing pipeline; a flagged row still counts as a session.
    warnings = detect_workout_quality_warnings(current_rows)
    excluded = {
        w["activity_id"] for w in warnings
        if w.get("action") == "excluded_from_pace_calcs" and w.get("activity_id")
    }

    current = summarize_sessions(
        current_rows, canonical, window_days=days, excluded_activity_ids=excluded
    )
    baseline = summarize_sessions(baseline_rows, canonical, window_days=baseline_days)
    series = weekly_series(current_rows, start_iso, end_iso)

    result: dict[str, Any] = {
        "activity_type": canonical,
        "requested_type": activity_type,
        "family": profile["family"],
        "available": bool(current_rows),
        "as_of": _now_iso(tz),
        "timezone": tz,
        "analysis_window": {
            "days": days, "start": start_iso, "end": end_iso,
            "boundary": "calendar days in the user timezone, today inclusive",
        },
        "baseline_window": {
            "days": baseline_days, "start": base_start_iso, "end": base_end_iso,
            "selection_method": (
                "the equally long window immediately preceding the analysis window"
            ),
        },
        "related_types_not_included": related_types(canonical),
        "supported_metrics": {
            "distance": profile["distance"],
            "pace_unit": profile["pace"],
            "speed_unit": profile["speed"],
            "delegates_to": profile["delegates_to"],
        },
        "summary": current,
        "baseline_summary": baseline,
        "comparison": compare(current, baseline, canonical),
        "weekly_series": series,
        "trends": weekly_trends(series),
        "records": observed_records(current_rows, canonical),
        "coverage": coverage_report(db, start_iso, end_iso),
        "data_quality": {
            "warnings": warnings,
            "excluded_from_distance_and_pace": sorted(excluded),
            "note": (
                "flagged sessions still count towards frequency and volume in "
                "time terms; they are only excluded from distance/pace maths"
            ),
        },
        "sessions_included": len(current_rows),
    }

    if profile["delegates_to"]:
        result["delegation"] = {
            "tool": profile["delegates_to"],
            "reason": (
                f"{profile['family']} progression needs exercise- or "
                "length-level detail this generic view does not model"
            ),
        }
    if not current_rows:
        result["unavailable_reason"] = (
            f"no {canonical} sessions recorded between {start_iso} and {end_iso}"
        )
    if not baseline_rows:
        result["comparison_note"] = (
            "no sessions of this type in the baseline window — changes are "
            "reported as unavailable rather than as growth from zero"
        )
    if include_sessions:
        result["sessions"] = [
            {
                "activity_id": s.get("activity_id"),
                "day": s.get("day"),
                "name": s.get("name"),
                "type": s.get("type"),
                "duration_s": _num(s.get("duration_s")),
                "distance_m": _num(s.get("distance_m")),
                "avg_hr": _num(s.get("avg_hr")),
                "training_load": _num(s.get("training_load")),
                "load_source": s.get("load_source"),
                "source": s.get("source"),
            }
            for s in sorted(current_rows, key=lambda r: r.get("day") or "", reverse=True)[
                : max(1, min(session_limit, 200))
            ]
        ]
        result["sessions_truncated"] = len(current_rows) > session_limit
    return result


def _now_iso(timezone_name: str | None) -> str:
    """Current timestamp in the user timezone — the report's ``as_of``."""
    try:
        return datetime.now(ZoneInfo(timezone_name)).isoformat() if timezone_name \
            else datetime.now().isoformat()
    except Exception:  # noqa: BLE001 — an unknown TZ must not break analytics
        return datetime.now().isoformat()


def recorded_activity_types(db, days: int = 90) -> list[dict[str, Any]]:
    """Every activity type actually recorded in the window, with its count.

    The combined progress report iterates this so a sport the user does (a
    swim, a row) can never be silently omitted just because nobody asked for
    it by name.
    """
    rows = db.recent_workouts(days=max(1, min(int(days), MAX_WINDOW_DAYS)))
    counts: dict[str, dict[str, Any]] = {}
    for row in rows:
        canonical = normalize_activity_type(row.get("type")) or "unknown"
        entry = counts.setdefault(canonical, {
            "activity_type": canonical,
            "family": activity_family(canonical),
            "workout_count": 0,
            "raw_types": set(),
        })
        entry["workout_count"] += 1
        if row.get("type"):
            entry["raw_types"].add(row["type"])
    out = []
    for entry in counts.values():
        entry["raw_types"] = sorted(entry["raw_types"])
        out.append(entry)
    return sorted(out, key=lambda e: (-e["workout_count"], e["activity_type"]))
