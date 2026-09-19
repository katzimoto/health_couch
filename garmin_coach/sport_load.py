"""Sport-specific training-load breakdown.

``get_training_load`` answers "how loaded am I?" with one EWMA acute:chronic
number over every activity. That is the right *monitoring* signal and it stays
exactly as it was — but it cannot say whether a rising load is swimming volume,
a running block or extra gym work. This module decomposes the same metric by
sport without changing what it means.

The rules, all pinned by tests:

* **The decomposition reconciles.** Per-sport load is the same
  ``Workout.training_load`` column, bucketed — so the buckets sum to the
  reported total exactly, and the response says so (``reconciles``). Anything
  that is *not* on that scale (strength working sets, exercise volume) is a
  separate supplemental block, never added to it.
* **Methods are not interchangeable.** Garmin's EPOC-based load, this repo's
  documented duration×type×intensity estimate, and a manually entered figure
  are reported with their split (``load_by_source``) and never presented as one
  homogeneous measurement.
* **Each sport gets its own guards.** A sport with too little history or a zero
  chronic load returns an unavailable ratio *with a reason* — not a divide by
  zero and not a borrowed number from the overall series.
* **Rest, missing sync and partial recordings are different things.** A synced
  day with no workout is confirmed rest; an unsynced day is unknown; a flagged
  recording is excluded and listed.
* **Ratios are descriptive.** An acute:chronic figure is a monitoring signal,
  not an injury prediction or a universal safe/unsafe threshold, and the
  response says that too.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .activity_progress import (
    MAX_WINDOW_DAYS,
    activity_family,
    coverage_report,
    metric_profile,
    normalize_activity_type,
    today_in,
    window_bounds,
)
from .analysis import _ACR_MIN_HISTORY_DAYS, _ACR_WARMUP_DAYS, _ewma

# A sport needs this many days between its first and last recorded session
# before its own chronic curve means anything. Same spirit as the analyzer's
# global guard, applied per sport so a single swim doesn't read as a spike.
SPORT_MIN_HISTORY_DAYS = _ACR_MIN_HISTORY_DAYS

# Acute/chronic EWMA spans, identical to the overall metric so the per-sport
# numbers are the same measurement, just partitioned.
ACUTE_SPAN_DAYS = 7
CHRONIC_SPAN_DAYS = 28

RATIO_NOTE = (
    "acute:chronic is a descriptive monitoring signal over your own history, "
    "not an injury prediction and not a universal safe/unsafe threshold"
)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def bucket_by_sport(workouts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group canonical workouts by normalized activity type.

    Uses the shared normalizer, so the buckets line up exactly with
    ``get_activity_progress``: the same modality distinctions, the same
    canonical keys, no second opinion about what counts as running.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for workout in workouts:
        key = normalize_activity_type(workout.get("type")) or "unknown"
        out.setdefault(key, []).append(workout)
    return out


def load_by_source(workouts: list[dict[str, Any]]) -> dict[str, Any]:
    """Split a bucket's load by how each figure was produced.

    ``garmin`` is the device's EPOC-based value; ``estimated`` is this repo's
    documented heuristic; ``manual`` is user-entered; ``none`` counts sessions
    carrying no load at all — which is missing data, not zero load.
    """
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    missing = 0
    for workout in workouts:
        load = _num(workout.get("training_load"))
        if load is None:
            missing += 1
            continue
        source = workout.get("load_source") or "unknown"
        totals[source] = round(totals.get(source, 0.0) + load, 2)
        counts[source] = counts.get(source, 0) + 1
    return {
        "totals": totals,
        "session_counts": counts,
        "sessions_without_load": missing,
        "note": (
            "garmin = device EPOC-based load; estimated = documented "
            "duration × type × intensity heuristic; manual = user-entered. "
            "They share a scale by construction but are not the same measurement"
        ),
    }


def daily_load_series(
    workouts: list[dict[str, Any]], start: date, end: date
) -> list[float]:
    """One load value per calendar day in ``[start, end]``, zero-filled.

    A day with no session genuinely contributed no load, so zero is correct
    here; whether that day was *rest* or *unsynced* is a separate question the
    coverage block answers.
    """
    by_day: dict[str, float] = {}
    for workout in workouts:
        day = workout.get("day")
        load = _num(workout.get("training_load")) or 0.0
        if day:
            by_day[day] = by_day.get(day, 0.0) + load
    span = (end - start).days + 1
    return [by_day.get((start + timedelta(days=i)).isoformat(), 0.0) for i in range(span)]


def acute_chronic(
    workouts: list[dict[str, Any]], start: date, end: date, *, label: str
) -> dict[str, Any]:
    """EWMA acute (7d) vs chronic (28d) load and their ratio, for one bucket.

    Withheld — with a stated reason — when the sport's own history is too short
    or its chronic load is zero. The spans and the EWMA are the analyzer's, so
    a single-sport athlete's per-sport ratio equals the overall one.
    """
    out: dict[str, Any] = {
        "acute_7d": None, "chronic_28d": None, "ratio": None,
        "method": (
            f"exponentially weighted moving averages, {ACUTE_SPAN_DAYS}-day acute "
            f"vs {CHRONIC_SPAN_DAYS}-day chronic span, over daily {label} load"
        ),
        "note": RATIO_NOTE,
    }
    days_with_load = [w for w in workouts if _num(w.get("training_load"))]
    if not days_with_load:
        out["unavailable_reason"] = f"no {label} session in the window carries a load value"
        return out

    # Seed the series at this sport's first recorded day, exactly as the
    # overall analyzer seeds at the first day with data: an EWMA seeded on a
    # long run of zeros before the sport existed would understate the chronic
    # curve and manufacture a spike. For a single-sport history this makes the
    # per-sport ratio identical to the overall one — which is the point of
    # calling it a decomposition.
    first_day = min(w["day"] for w in days_with_load if w.get("day"))
    series_start = max(start, date.fromisoformat(first_day))
    series = daily_load_series(workouts, series_start, end)
    out["acute_7d"] = round(_ewma(series, ACUTE_SPAN_DAYS), 2)
    out["chronic_28d"] = round(_ewma(series, CHRONIC_SPAN_DAYS), 2)

    session_days = sorted({w["day"] for w in days_with_load if w.get("day")})
    history_days = (
        (date.fromisoformat(session_days[-1]) - date.fromisoformat(session_days[0])).days + 1
        if session_days else 0
    )
    if history_days < SPORT_MIN_HISTORY_DAYS:
        out["unavailable_reason"] = (
            f"{label} history spans {history_days} day(s); a chronic load needs at "
            f"least {SPORT_MIN_HISTORY_DAYS} to mean anything"
        )
        return out
    if not out["chronic_28d"]:
        out["unavailable_reason"] = (
            f"{label} chronic load is zero — a ratio would divide by zero"
        )
        return out
    out["ratio"] = round(out["acute_7d"] / out["chronic_28d"], 2)
    return out


def strength_supplement(
    workouts: list[dict[str, Any]], sessions: list[dict[str, Any]], window_days: int
) -> dict[str, Any]:
    """Strength workload in *strength* units — deliberately not cardio load.

    Session frequency, completed working sets and per-exercise volume stay in
    their own block: kilograms × reps is not comparable with an EPOC-based load
    score or with kilometres, and adding them would be meaningless. This block
    also means strength workload stays visible when the watch recorded no HR or
    load at all.
    """
    from .strength_progress import session_performance

    weeks = max(window_days / 7.0, 1 / 7.0)
    completed_sets = 0
    volume = 0.0
    volume_sessions = 0
    exercises = 0
    exact_volume = True
    for session in sessions:
        session_volume = 0.0
        has_volume = False
        for exercise in session.get("exercises") or []:
            performance = session_performance({
                **exercise,
                "date": session.get("day"),
                "session_id": session.get("id"),
            })
            if not performance["counts_towards_volume"]:
                continue
            exercises += 1
            if performance["sets"]:
                completed_sets += performance["sets"]
            if performance["completed_volume_kg"] is not None:
                session_volume += performance["completed_volume_kg"]
                has_volume = True
                if performance["volume_basis"] != "per_set":
                    exact_volume = False
        if has_volume:
            volume += session_volume
            volume_sessions += 1

    return {
        "sessions": len(workouts),
        "sessions_per_week": round(len(workouts) / weeks, 2),
        "logged_strength_sessions": len(sessions),
        "completed_working_sets": completed_sets,
        "exercises_performed": exercises,
        "completed_volume_kg": round(volume, 1) if volume_sessions else None,
        "weekly_volume_kg": round(volume / weeks, 1) if volume_sessions else None,
        "volume_sessions": volume_sessions,
        "volume_basis": (
            None if not volume_sessions
            else "per_set" if exact_volume else "mixed_per_set_and_aggregate_estimate"
        ),
        "units": {"completed_volume_kg": "kilograms × reps", "working_sets": "sets"},
        "note": (
            "strength workload is reported in its own units and is never added "
            "to cardiovascular load, distance or a provider load score; it stays "
            "visible even when the device recorded no heart rate or load"
        ),
    }


def rest_and_coverage(
    db, workouts: list[dict[str, Any]], start_iso: str, end_iso: str
) -> dict[str, Any]:
    """Distinguish confirmed rest from missing ingestion.

    A day the scheduler successfully pulled, with no workout recorded, is
    *confirmed rest*. A day with no recorded pull is *unknown* — the watch may
    simply not have synced. Reporting them as one number would turn a sync
    outage into a training-load drop.
    """
    coverage = coverage_report(db, start_iso, end_iso)
    unsynced = set(coverage.get("unsynced_day_list") or [])
    active_days = {w["day"] for w in workouts if w.get("day")}
    start, end = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    all_days = [
        (start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)
    ]
    confirmed_rest = [
        d for d in all_days if d not in active_days and d not in unsynced
    ]
    unknown = [d for d in all_days if d not in active_days and d in unsynced]
    return {
        **coverage,
        "active_days": len(active_days),
        "confirmed_rest_days": len(confirmed_rest),
        "unknown_days": len(unknown),
        "unknown_day_list": unknown[:30],
        "distinction": (
            "confirmed rest = a day that synced with no session recorded; "
            "unknown = a day with no recorded sync at all"
        ),
    }


# ── The service ───────────────────────────────────────────────────────────────


def build_sport_training_load(
    db,
    days: int = 28,
    sport: str | None = None,
    *,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """Per-sport decomposition of the training-load metric over ``days``.

    The overall acute:chronic figure is unchanged and still reported under
    ``overall``; ``by_sport`` splits the same ``training_load`` column with the
    same EWMA spans. ``reconciliation`` proves the split adds up.
    """
    from .analysis import Analyzer
    from .config import settings
    from .workout_quality import detect_findings, record_blocking_ids

    tz = timezone_name or settings.timezone
    days = max(1, min(int(days), MAX_WINDOW_DAYS))
    end = today_in(tz)
    start_iso, end_iso = window_bounds(days, end)

    # Warm-up history so the chronic EWMA isn't dragged by its seed value, the
    # same trick the overall analyzer uses.
    warmup_days = max(days, _ACR_WARMUP_DAYS)
    warm_start_iso, _ = window_bounds(warmup_days, end)
    all_rows = db.recent_workouts(days=warmup_days)

    window_rows = [w for w in all_rows if start_iso <= (w.get("day") or "") <= end_iso]
    findings = detect_findings(window_rows)
    blocked = record_blocking_ids(findings)

    buckets = bucket_by_sport(window_rows)
    warm_buckets = bucket_by_sport(all_rows)
    strength_sessions = db.recent_strength_sessions(days=days)

    requested = normalize_activity_type(sport) if sport else None
    weeks = max(days / 7.0, 1 / 7.0)

    by_sport: dict[str, Any] = {}
    bucket_total = 0.0
    for key, rows in sorted(buckets.items()):
        if requested and key != requested:
            continue
        family = activity_family(key)
        profile = metric_profile(key)
        countable = [w for w in rows if w.get("activity_id") not in blocked]
        excluded = [w["activity_id"] for w in rows if w.get("activity_id") in blocked]
        total_load = sum(_num(w.get("training_load")) or 0.0 for w in countable)
        bucket_total += total_load
        duration = sum(_num(w.get("duration_s")) or 0.0 for w in countable)
        distance = sum(_num(w.get("distance_m")) or 0.0 for w in countable)
        has_distance = profile["distance"] and any(
            _num(w.get("distance_m")) for w in countable
        )

        entry: dict[str, Any] = {
            "activity_type": key,
            "family": family,
            "session_count": len(countable),
            "sessions_per_week": round(len(countable) / weeks, 2),
            "total_duration_s": round(duration, 1),
            "weekly_duration_s": round(duration / weeks, 1),
            "total_distance_m": round(distance, 1) if has_distance else None,
            "weekly_distance_m": round(distance / weeks, 1) if has_distance else None,
            "distance_unavailable_reason": (
                None if has_distance else
                f"{family} activities do not record a comparable distance"
                if not profile["distance"] else "no session recorded a distance"
            ),
            "total_training_load": round(total_load, 1),
            "weekly_training_load": round(total_load / weeks, 1),
            "load_by_source": load_by_source(countable),
            "acute_chronic": acute_chronic(
                [w for w in warm_buckets.get(key, []) if w.get("activity_id") not in blocked],
                date.fromisoformat(warm_start_iso), end, label=key,
            ),
            "excluded_activity_ids": sorted(excluded),
            "coverage": {
                "sessions_with_load": sum(
                    1 for w in countable if _num(w.get("training_load")) is not None
                ),
                "sessions_total": len(countable),
                "note": (
                    "a session without a load value is missing data, not zero load"
                ),
            },
            "activity_ids": sorted(
                w["activity_id"] for w in countable if w.get("activity_id") is not None
            )[:50],
        }
        if family == "strength":
            entry["strength_workload"] = strength_supplement(
                countable, strength_sessions, days
            )
        by_sport[key] = entry

    analyzer = Analyzer(db)
    overall = analyzer.acute_chronic_ratio()
    reported_total = round(
        sum(
            _num(w.get("training_load")) or 0.0
            for w in window_rows if w.get("activity_id") not in blocked
        ),
        1,
    )

    result: dict[str, Any] = {
        "window": {"days": days, "start": start_iso, "end": end_iso,
                   "boundary": "calendar days in the user timezone, today inclusive"},
        "timezone": tz,
        "filtered_to_sport": requested,
        "overall": {
            **overall,
            "total_training_load_in_window": reported_total,
            "weekly_training_load": round(reported_total / weeks, 1),
            "method": (
                "unchanged legacy metric: EWMA over the summed daily "
                "training_load of every canonical workout"
            ),
            "note": RATIO_NOTE,
        },
        "by_sport": by_sport,
        "reconciliation": {
            "sum_of_sport_load": round(bucket_total, 1),
            "reported_total_load": reported_total,
            "reconciles": (
                abs(bucket_total - reported_total) < 0.05 if requested is None else None
            ),
            "composition": (
                "sport buckets partition the same training_load column, so they "
                "sum to the total exactly; supplemental strength metrics "
                "(working sets, kg × reps) are a separate, non-additive block"
            ),
            "not_summed": ["strength_workload"],
            **({"note": "filtered to one sport — the sum is that sport only"}
               if requested else {}),
        },
        "rest_and_coverage": rest_and_coverage(db, window_rows, start_iso, end_iso),
        "data_quality": {
            "findings": findings,
            "excluded_activity_ids": sorted(blocked),
            "note": (
                "recordings that are incomplete or physically inconsistent are "
                "excluded from every load figure above and listed here"
            ),
        },
        "sports_recorded": sorted(buckets),
    }
    if requested and requested not in by_sport:
        result["unavailable_reason"] = (
            f"no {requested} sessions recorded between {start_iso} and {end_iso}"
        )
    return result
