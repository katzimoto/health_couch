"""Swimming analytics — pure functions over stored swim detail.

A swim summary alone cannot tell a coach whether the swimmer got *faster*: an
equal-distance session can be quicker because the swimming was faster, because
the rests were shorter, or because the effort was higher. Those are different
conclusions, so this module keeps them apart:

* **Time semantics are preserved, never interchanged.** Garmin reports elapsed,
  timer and moving/active durations separately. Rest is only derived when two
  *documented, compatible* fields support it (elapsed − active, or the sum of
  provider-marked rest intervals); otherwise ``rest_duration_s`` stays None and
  the reason is reported.
* **Active pace and elapsed pace are separate series.** An elapsed-time-only
  record is labelled as such, and never gets an invented active pace, SWOLF,
  stroke efficiency or continuous PR.
* **Continuous bests come from contiguous lengths only.** A best 100 m is the
  fastest run of adjacent, non-rest lengths that sums to *exactly* 100 m in the
  same stroke and pool. Nothing is interpolated from a whole-session average and
  nothing is bridged across a rest, a recording gap or a separate fragment.
* **Like-for-like comparisons only.** Pool length (in its recorded unit),
  stroke and workout structure form the comparability key; yards and metres are
  converted for distance totals but efficiency metrics (SWOLF, strokes per
  length) are never pooled across different pool lengths.

Everything here is a pure function over plain dicts, so the swim maths is
unit-testable without a database, a clock or a network.
"""

from __future__ import annotations

from typing import Any, Iterable

# Pool-length units Garmin reports, and their metre factor.
_UNIT_METERS = {
    "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0, "m": 1.0,
    "yard": 0.9144, "yards": 0.9144, "yd": 0.9144,
    "kilometer": 1000.0, "kilometre": 1000.0, "km": 1000.0,
    "mile": 1609.344, "mi": 1609.344,
}

# Continuous-effort distances worth reporting, in metres. Only produced when
# contiguous lengths sum to the distance exactly.
CONTINUOUS_EFFORT_DISTANCES_M = (50.0, 100.0, 200.0, 400.0)

# Two lengths are treated as contiguous when the provider's own ordering puts
# them next to each other and neither is a rest interval. A tolerance of a few
# centimetres absorbs float noise in converted yard pools.
_DISTANCE_TOLERANCE_M = 0.05


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def normalize_stroke(raw: Any) -> str | None:
    """Canonical stroke name, or None when the provider didn't record one.

    ``"FREESTYLE"``/``"free"`` → ``freestyle``. Unknown labels pass through
    lowercased rather than being forced into a known stroke.
    """
    if raw is None:
        return None
    text = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if not text or text in ("unknown", "none", "null"):
        return None
    aliases = {
        "free": "freestyle", "freestyle": "freestyle", "front_crawl": "freestyle",
        "back": "backstroke", "backstroke": "backstroke",
        "breast": "breaststroke", "breaststroke": "breaststroke",
        "fly": "butterfly", "butterfly": "butterfly",
        "im": "individual_medley", "individual_medley": "individual_medley",
        "mixed": "mixed", "drill": "drill", "rest": "rest",
    }
    return aliases.get(text, text)


def pool_length_meters(value: Any, unit: Any) -> float | None:
    """Pool length in metres from the provider's value + unit key.

    Returns None (never a guess) when the unit is missing or unrecognised — a
    25 that might be metres or yards is not a length we can compare against
    another session.
    """
    raw = _num(value)
    if raw is None or raw <= 0:
        return None
    key = str(unit or "").strip().lower()
    factor = _UNIT_METERS.get(key)
    if factor is None:
        return None
    return round(raw * factor, 4)


def comparability_key(session: dict[str, Any]) -> tuple[Any, ...]:
    """The grouping key for like-for-like swim comparison.

    Pool length *as recorded* (value + unit) and the primary stroke: a 25 yd
    session and a 25 m session are not comparable for efficiency metrics even
    though both are "25".
    """
    return (
        session.get("pool_length_raw"),
        (session.get("pool_length_unit") or "").lower() or None,
        session.get("primary_stroke"),
    )


# ── Length-level derivations ──────────────────────────────────────────────────


def split_active_and_rest(lengths: list[dict[str, Any]]) -> dict[str, Any]:
    """Split recorded lengths into active swimming and provider-marked rest.

    Returns counts and durations for each, plus ``rest_source`` naming how the
    rest figure was obtained. When the provider marks no rest intervals at all,
    rest is *not* inferred here — :func:`session_time_breakdown` decides whether
    two compatible duration fields support deriving it.
    """
    active = [l for l in lengths if not l.get("is_rest")]
    rest = [l for l in lengths if l.get("is_rest")]
    active_s = sum(_num(l.get("duration_s")) or 0.0 for l in active)
    rest_s = sum(_num(l.get("duration_s")) or 0.0 for l in rest)
    return {
        "active_lengths": len(active),
        "rest_intervals": len(rest),
        "active_duration_s": round(active_s, 1) if active else None,
        "rest_duration_s": round(rest_s, 1) if rest else None,
        "rest_source": "provider-marked rest intervals" if rest else None,
        "distance_m": round(sum(_num(l.get("distance_m")) or 0.0 for l in active), 2) or None,
    }


def session_time_breakdown(
    *,
    elapsed_s: float | None,
    timer_s: float | None,
    active_s: float | None,
    lengths_rest_s: float | None = None,
    lengths_active_s: float | None = None,
) -> dict[str, Any]:
    """Reconcile the several clocks a swim can be measured with.

    Only *documented compatible* pairs produce a rest figure:
    provider-marked rest intervals, or elapsed − active (both present). Timer
    time is reported but never silently substituted for active time. When no
    pair supports it, ``rest_duration_s`` is None with a stated reason, and
    ``active_time_available`` is False so callers know not to claim an active
    pace.
    """
    active = active_s if active_s is not None else lengths_active_s
    out: dict[str, Any] = {
        "elapsed_duration_s": elapsed_s,
        "timer_duration_s": timer_s,
        "active_duration_s": active,
        "active_time_available": active is not None,
        "rest_duration_s": None,
        "rest_source": None,
    }
    if lengths_rest_s is not None:
        out["rest_duration_s"] = round(lengths_rest_s, 1)
        out["rest_source"] = "sum of provider-marked rest intervals"
    elif elapsed_s is not None and active is not None and elapsed_s >= active:
        out["rest_duration_s"] = round(elapsed_s - active, 1)
        out["rest_source"] = "elapsed_duration − active_duration"
    else:
        out["rest_unavailable_reason"] = (
            "no compatible pair of recorded durations supports a rest figure "
            "(need marked rest intervals, or elapsed and active time together)"
        )
    if active is None:
        out["active_time_unavailable_reason"] = (
            "the provider recorded only elapsed/timer duration for this session"
        )
    return out


def contiguous_runs(lengths: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Maximal runs of adjacent, non-rest lengths in the same stroke.

    A rest interval, a stroke change or a gap in the provider's ``length_index``
    ends a run. Runs are the *only* basis for a continuous-effort best: they are
    exactly the stretches the swimmer actually swam without stopping.
    """
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for length in sorted(lengths, key=lambda l: (_num(l.get("length_index")) or 0)):
        if length.get("is_rest"):
            previous = None
            if current:
                runs.append(current)
                current = []
            continue
        if previous is not None:
            index_gap = (_num(length.get("length_index")) or 0) - (
                _num(previous.get("length_index")) or 0
            )
            stroke_changed = (
                length.get("stroke") is not None
                and previous.get("stroke") is not None
                and length["stroke"] != previous["stroke"]
            )
            if index_gap != 1 or stroke_changed:
                if current:
                    runs.append(current)
                current = []
        current.append(length)
        previous = length
    if current:
        runs.append(current)
    return runs


def best_continuous_effort(
    lengths: list[dict[str, Any]], distance_m: float
) -> dict[str, Any] | None:
    """Fastest contiguous stretch covering *exactly* ``distance_m``, or None.

    Walks each contiguous run with a sliding window and only accepts a window
    whose summed length distance equals the target (within float tolerance).
    Nothing is interpolated: a 25 m pool can support 50/100/200 m bests, a 33 ⅓ m
    pool cannot support 100 m, and a session with no length data supports none.
    """
    best: dict[str, Any] | None = None
    for run in contiguous_runs(lengths):
        distances = [_num(l.get("distance_m")) or 0.0 for l in run]
        durations = [_num(l.get("duration_s")) for l in run]
        for start in range(len(run)):
            total_d = 0.0
            total_t = 0.0
            for end in range(start, len(run)):
                if durations[end] is None or distances[end] <= 0:
                    break  # an unusable length can't be part of a timed effort
                total_d += distances[end]
                total_t += durations[end]
                if total_d > distance_m + _DISTANCE_TOLERANCE_M:
                    break
                if abs(total_d - distance_m) <= _DISTANCE_TOLERANCE_M:
                    if best is None or total_t < best["duration_s"]:
                        best = {
                            "distance_m": distance_m,
                            "duration_s": round(total_t, 1),
                            "pace_s_per_100m": round(total_t / (total_d / 100.0), 1),
                            "from_length_index": run[start].get("length_index"),
                            "to_length_index": run[end].get("length_index"),
                            "lengths": end - start + 1,
                            "stroke": run[start].get("stroke"),
                            "basis": "contiguous recorded lengths, no rest bridged",
                        }
                    break
    return best


def continuous_bests(lengths: list[dict[str, Any]]) -> dict[str, Any]:
    """Best continuous efforts at the standard distances, with reasons.

    Every distance the length data cannot support exactly comes back with an
    explicit reason instead of an approximation.
    """
    out: dict[str, Any] = {"available": bool(lengths), "efforts": {}, "unsupported": {}}
    if not lengths:
        out["unavailable_reason"] = (
            "no per-length data recorded for this session — continuous bests "
            "cannot be derived from whole-session averages"
        )
        return out
    for distance in CONTINUOUS_EFFORT_DISTANCES_M:
        effort = best_continuous_effort(lengths, distance)
        key = f"{int(distance)}m"
        if effort is None:
            out["unsupported"][key] = (
                "no contiguous run of recorded lengths sums to exactly "
                f"{int(distance)} m in one stroke"
            )
        else:
            out["efforts"][key] = effort
    return out


def efficiency_metrics(
    lengths: list[dict[str, Any]], detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    """SWOLF, strokes per length and distance per stroke, where supported.

    Computed from the length rows when they exist (the honest source), falling
    back to the provider's session averages when they don't. Each figure names
    its source and sample count; none is invented from distance and time alone.
    """
    detail = detail or {}
    active = [l for l in lengths if not l.get("is_rest")]
    swolfs = [_num(l.get("swolf")) for l in active]
    swolfs = [s for s in swolfs if s is not None]
    strokes = [_num(l.get("strokes")) for l in active]
    strokes = [s for s in strokes if s is not None]
    distances = [
        _num(l.get("distance_m")) for l in active if _num(l.get("distance_m"))
    ]

    out: dict[str, Any] = {}
    if swolfs:
        out["avg_swolf"] = {
            "value": round(sum(swolfs) / len(swolfs), 1),
            "source": "per-length records",
            "sample_count": len(swolfs),
            "note": "SWOLF is only comparable within the same pool length and stroke",
        }
    elif _num(detail.get("avg_swolf")) is not None:
        out["avg_swolf"] = {
            "value": _num(detail["avg_swolf"]),
            "source": "provider session average",
            "sample_count": None,
        }
    else:
        out["avg_swolf"] = None
        out["avg_swolf_unavailable_reason"] = "no SWOLF recorded for this session"

    if strokes:
        out["avg_strokes_per_length"] = {
            "value": round(sum(strokes) / len(strokes), 1),
            "source": "per-length records",
            "sample_count": len(strokes),
        }
        total_strokes = sum(strokes)
        total_distance = sum(distances) if distances else None
        if total_distance and total_strokes:
            out["distance_per_stroke_m"] = {
                "value": round(total_distance / total_strokes, 3),
                "source": "per-length records",
                "method": "total active distance ÷ total strokes",
            }
    else:
        out["avg_strokes_per_length"] = None
        if _num(detail.get("avg_stroke_distance_m")) is not None:
            out["distance_per_stroke_m"] = {
                "value": _num(detail["avg_stroke_distance_m"]),
                "source": "provider session average",
                "method": "provider-reported average stroke distance",
            }
        else:
            out["distance_per_stroke_m"] = None
            out["efficiency_unavailable_reason"] = (
                "no stroke counts recorded — stroke efficiency cannot be derived "
                "from distance and time alone"
            )
    return out


def stroke_mix(lengths: list[dict[str, Any]]) -> dict[str, int]:
    """Lengths swum per stroke — the basis for "mixed strokes" handling."""
    mix: dict[str, int] = {}
    for length in lengths:
        if length.get("is_rest"):
            continue
        stroke = length.get("stroke") or "unknown"
        mix[stroke] = mix.get(stroke, 0) + 1
    return dict(sorted(mix.items(), key=lambda kv: (-kv[1], kv[0])))


def build_session(
    workout: dict[str, Any],
    detail: dict[str, Any] | None,
    lengths: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """One swim session in the analytics schema, from its stored rows.

    ``workout`` is the canonical workout row, ``detail`` its
    :class:`~garmin_coach.models.ActivityDetail` (or None when detail was never
    ingested), ``lengths`` its recorded lengths (possibly empty). Every derived
    figure is marked available/unavailable with a reason — an elapsed-only
    record never acquires an active pace.
    """
    detail = detail or {}
    lengths = lengths or []
    length_split = split_active_and_rest(lengths)

    distance_m = _num(workout.get("distance_m")) or length_split.get("distance_m")
    elapsed = _num(detail.get("elapsed_duration_s")) or _num(workout.get("duration_s"))
    timer = _num(detail.get("timer_duration_s"))
    active = _num(detail.get("active_duration_s"))
    timing = session_time_breakdown(
        elapsed_s=elapsed,
        timer_s=timer,
        active_s=active,
        lengths_rest_s=length_split.get("rest_duration_s"),
        lengths_active_s=length_split.get("active_duration_s"),
    )

    pool_m = _num(detail.get("pool_length_m"))
    session: dict[str, Any] = {
        "activity_id": workout.get("activity_id"),
        "day": workout.get("day"),
        "name": workout.get("name"),
        "type": workout.get("type"),
        "source": workout.get("source"),
        "start_time": workout.get("start_time"),
        "distance_m": round(distance_m, 1) if distance_m else None,
        "pool_length_m": pool_m,
        "pool_length_raw": _num(detail.get("pool_length_raw")),
        "pool_length_unit": detail.get("pool_length_unit"),
        "primary_stroke": detail.get("primary_stroke"),
        "avg_hr": _num(workout.get("avg_hr")),
        "max_hr": _num(workout.get("max_hr")),
        "calories": _num(workout.get("calories")),
        "training_load": _num(workout.get("training_load")),
        "load_source": workout.get("load_source"),
        "detail_status": detail.get("status") or "not_ingested",
        "lengths_recorded": len(lengths),
        **timing,
        "active_lengths": length_split["active_lengths"],
        "rest_intervals": length_split["rest_intervals"],
        "stroke_mix": stroke_mix(lengths) or None,
    }

    # Pace: elapsed pace is almost always derivable; active pace only when an
    # active clock actually exists.
    session["elapsed_pace_s_per_100m"] = (
        round(elapsed / (distance_m / 100.0), 1)
        if distance_m and elapsed and distance_m > 0 and elapsed > 0 else None
    )
    if timing["active_time_available"] and distance_m and timing["active_duration_s"]:
        session["active_pace_s_per_100m"] = round(
            timing["active_duration_s"] / (distance_m / 100.0), 1
        )
    else:
        session["active_pace_s_per_100m"] = None
        session["active_pace_unavailable_reason"] = (
            timing.get("active_time_unavailable_reason")
            or "no distance recorded for this session"
        )

    session["efficiency"] = efficiency_metrics(lengths, detail)
    session["continuous_bests"] = continuous_bests(lengths)
    session["comparability_key"] = list(comparability_key(session))
    if pool_m is None and lengths:
        session["pool_length_note"] = (
            "pool length not recorded in a known unit — efficiency metrics are "
            "not pooled with other sessions"
        )
    return session


# ── Window-level aggregation ──────────────────────────────────────────────────


def _mean(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 1) if clean else None


def aggregate_sessions(sessions: list[dict[str, Any]], window_days: int) -> dict[str, Any]:
    """Volume, separated pace series, rest and efficiency over one window.

    Active pace and elapsed pace are aggregated over *their own* eligible
    sessions (weighted, totals over totals) and reported separately with their
    sample counts, so a change in rest can never masquerade as a change in
    swimming speed.
    """
    weeks = max(window_days / 7.0, 1 / 7.0)
    count = len(sessions)
    total_distance = sum(s.get("distance_m") or 0.0 for s in sessions)
    total_elapsed = sum(s.get("elapsed_duration_s") or 0.0 for s in sessions)

    active_rows = [
        s for s in sessions
        if s.get("active_duration_s") and (s.get("distance_m") or 0) > 0
    ]
    active_distance = sum(s["distance_m"] for s in active_rows)
    active_time = sum(s["active_duration_s"] for s in active_rows)

    elapsed_rows = [
        s for s in sessions
        if s.get("elapsed_duration_s") and (s.get("distance_m") or 0) > 0
    ]
    elapsed_distance = sum(s["distance_m"] for s in elapsed_rows)
    elapsed_time = sum(s["elapsed_duration_s"] for s in elapsed_rows)

    rest_rows = [s for s in sessions if s.get("rest_duration_s") is not None]
    hr_rows = [s for s in sessions if s.get("avg_hr") is not None]

    out: dict[str, Any] = {
        "session_count": count,
        "sessions_per_week": round(count / weeks, 2),
        "total_distance_m": round(total_distance, 1) if count else 0.0,
        "weekly_distance_m": round(total_distance / weeks, 1) if count else 0.0,
        "total_elapsed_duration_s": round(total_elapsed, 1) if count else 0.0,
        "weekly_duration_s": round(total_elapsed / weeks, 1) if count else 0.0,
        "avg_session_distance_m": round(total_distance / count, 1) if count else None,
        "active_pace_s_per_100m": (
            round(active_time / (active_distance / 100.0), 1)
            if active_rows and active_distance > 0 else None
        ),
        "active_pace_sample_count": len(active_rows),
        "elapsed_pace_s_per_100m": (
            round(elapsed_time / (elapsed_distance / 100.0), 1)
            if elapsed_rows and elapsed_distance > 0 else None
        ),
        "elapsed_pace_sample_count": len(elapsed_rows),
        "avg_rest_duration_s": _mean([s.get("rest_duration_s") for s in rest_rows]),
        "rest_sample_count": len(rest_rows),
        "avg_hr": _mean([s.get("avg_hr") for s in hr_rows]),
        "hr_sample_count": len(hr_rows),
        "pace_method": "total distance ÷ total time over eligible sessions",
    }
    if not active_rows:
        out["active_pace_unavailable_reason"] = (
            "no session in this window recorded an active/moving swim time"
        )
    if not rest_rows:
        out["rest_unavailable_reason"] = (
            "no session in this window supports a rest figure from compatible "
            "recorded durations"
        )

    longest = max(
        (s for s in sessions if (s.get("distance_m") or 0) > 0),
        key=lambda s: s["distance_m"],
        default=None,
    )
    out["longest_session"] = (
        {
            "activity_id": longest["activity_id"],
            "day": longest["day"],
            "distance_m": longest["distance_m"],
            "elapsed_duration_s": longest.get("elapsed_duration_s"),
        }
        if longest else None
    )
    return out


def efficiency_by_comparability(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Efficiency grouped by pool length + stroke — never pooled across them.

    Each group reports its own SWOLF / strokes-per-length averages and the
    sessions behind them, so a switch from a 25 m to a 50 m pool shows up as two
    groups rather than an unexplained efficiency "drop".
    """
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for session in sessions:
        key = comparability_key(session)
        group = groups.setdefault(key, {
            "pool_length_raw": key[0],
            "pool_length_unit": key[1],
            "stroke": key[2],
            "session_count": 0,
            "activity_ids": [],
            "_swolf": [],
            "_strokes": [],
            "_dps": [],
        })
        group["session_count"] += 1
        group["activity_ids"].append(session.get("activity_id"))
        efficiency = session.get("efficiency") or {}
        if efficiency.get("avg_swolf"):
            group["_swolf"].append(efficiency["avg_swolf"]["value"])
        if efficiency.get("avg_strokes_per_length"):
            group["_strokes"].append(efficiency["avg_strokes_per_length"]["value"])
        if efficiency.get("distance_per_stroke_m"):
            group["_dps"].append(efficiency["distance_per_stroke_m"]["value"])

    out: list[dict[str, Any]] = []
    for group in groups.values():
        entry = {
            "pool_length_raw": group["pool_length_raw"],
            "pool_length_unit": group["pool_length_unit"],
            "stroke": group["stroke"],
            "session_count": group["session_count"],
            "activity_ids": group["activity_ids"],
            "avg_swolf": _mean(group["_swolf"]),
            "avg_strokes_per_length": _mean(group["_strokes"]),
            "avg_distance_per_stroke_m": (
                round(sum(group["_dps"]) / len(group["_dps"]), 3) if group["_dps"] else None
            ),
            "comparable": group["pool_length_raw"] is not None and group["stroke"] is not None,
        }
        if not entry["comparable"]:
            entry["note"] = (
                "pool length or stroke unrecorded — this group is reported "
                "separately and never merged with identified sessions"
            )
        out.append(entry)
    return sorted(out, key=lambda g: (-g["session_count"], str(g["pool_length_raw"])))


def window_continuous_bests(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Best continuous effort per distance across a window of sessions.

    Only sessions that actually carry contiguous length data contribute; the
    result names the session each best came from.
    """
    bests: dict[str, Any] = {}
    supported = 0
    for session in sessions:
        session_bests = (session.get("continuous_bests") or {}).get("efforts") or {}
        if session_bests:
            supported += 1
        for key, effort in session_bests.items():
            current = bests.get(key)
            if current is None or effort["duration_s"] < current["duration_s"]:
                bests[key] = {
                    **effort,
                    "activity_id": session.get("activity_id"),
                    "day": session.get("day"),
                }
    return {
        "efforts": bests,
        "sessions_with_length_data": supported,
        "sessions_total": len(sessions),
        "basis": (
            "fastest contiguous run of recorded lengths summing to exactly the "
            "target distance; rests and recording gaps are never bridged"
        ),
        "available": bool(bests),
        **(
            {}
            if bests
            else {
                "unavailable_reason": (
                    "no session in this window has contiguous length data "
                    "supporting an exact standard distance"
                )
            }
        ),
    }


# ── The service ───────────────────────────────────────────────────────────────

# Swim modalities. Pool and open-water are reported separately (they are not
# comparable) but both are included, so an open-water block can never vanish.
SWIM_TYPES = ("lap_swimming", "open_water_swimming")


def _pct(baseline: float | None, current: float | None) -> float | None:
    from .activity_progress import percent_change

    return percent_change(baseline, current)


def _compare_metric(
    current: float | None,
    baseline: float | None,
    *,
    lower_is_better: bool,
    unit: str,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """One before/after comparison with its direction made explicit."""
    out: dict[str, Any] = {
        "current": current,
        "baseline": baseline,
        "unit": unit,
        "direction": "lower_is_better" if lower_is_better else "higher_is_more",
        "absolute_change": (
            round(current - baseline, 1)
            if current is not None and baseline is not None else None
        ),
        "percent_change": _pct(baseline, current),
    }
    if out["absolute_change"] is None:
        out["unavailable_reason"] = unavailable_reason or (
            "not recorded in one or both windows"
        )
    else:
        improved = out["absolute_change"] < 0 if lower_is_better else out["absolute_change"] > 0
        out["improved"] = improved
    return out


def build_swimming_progress(
    db,
    days: int = 90,
    *,
    baseline_days: int | None = None,
    pool_length_m: float | None = None,
    stroke: str | None = None,
    min_distance_m: float | None = None,
    include_sessions: bool = False,
    session_limit: int = 25,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """Swimming progression over ``days``, built from stored swim detail.

    Reuses the shared aggregation contract in
    :mod:`~garmin_coach.activity_progress` for windows, calendar weeks and
    coverage, so swim totals can never disagree with
    ``get_activity_progress("lap_swimming")``. What it adds is everything the
    generic view cannot model: the separate active/elapsed clocks, rest time,
    per-length efficiency and exact continuous-effort bests.

    Filters (``pool_length_m``, ``stroke``, ``min_distance_m``) narrow the
    sessions to a comparable set; what was filtered out is reported, never
    silently dropped.
    """
    from .activity_progress import (
        MAX_WINDOW_DAYS,
        coverage_report,
        normalize_activity_type,
        today_in,
        weekly_series,
        weekly_trends,
        window_bounds,
    )
    from .config import settings
    from datetime import date as _date, timedelta as _timedelta

    tz = timezone_name or settings.timezone
    days = max(1, min(int(days), MAX_WINDOW_DAYS))
    baseline_days = max(1, min(int(baseline_days or days), MAX_WINDOW_DAYS))
    end = today_in(tz)
    start_iso, end_iso = window_bounds(days, end)
    base_end = _date.fromisoformat(start_iso) - _timedelta(days=1)
    base_start_iso, base_end_iso = window_bounds(baseline_days, base_end)

    span = (end - _date.fromisoformat(base_start_iso)).days + 1
    rows = db.recent_workouts(days=min(span, MAX_WINDOW_DAYS))
    swims = [
        r for r in rows
        if (normalize_activity_type(r.get("type")) or "") in SWIM_TYPES
        or "swim" in (normalize_activity_type(r.get("type")) or "")
    ]
    ids = [r["activity_id"] for r in swims]
    details = db.activity_details(ids)
    lengths = db.activity_lengths(ids)

    sessions = [
        build_session(row, details.get(row["activity_id"]), lengths.get(row["activity_id"]))
        for row in swims
    ]

    # ── Filters, with what they removed reported ─────────────────────────────
    filtered_out: list[dict[str, Any]] = []

    def keep(session: dict[str, Any]) -> bool:
        if pool_length_m is not None:
            actual = session.get("pool_length_m")
            if actual is None or abs(actual - float(pool_length_m)) > 0.5:
                filtered_out.append({
                    "activity_id": session["activity_id"],
                    "reason": "pool length does not match the requested filter"
                    if actual is not None else "pool length not recorded",
                })
                return False
        if stroke is not None:
            wanted = normalize_stroke(stroke)
            if session.get("primary_stroke") != wanted:
                filtered_out.append({
                    "activity_id": session["activity_id"],
                    "reason": "primary stroke does not match the requested filter",
                })
                return False
        if min_distance_m is not None and (session.get("distance_m") or 0) < float(min_distance_m):
            filtered_out.append({
                "activity_id": session["activity_id"],
                "reason": "below the requested minimum distance",
            })
            return False
        return True

    kept = [s for s in sessions if keep(s)]
    current = [s for s in kept if start_iso <= (s.get("day") or "") <= end_iso]
    baseline = [s for s in kept if base_start_iso <= (s.get("day") or "") <= base_end_iso]

    current_agg = aggregate_sessions(current, days)
    baseline_agg = aggregate_sessions(baseline, baseline_days)

    series_rows = [
        {
            "day": s["day"],
            "distance_m": s.get("distance_m") or 0.0,
            "duration_s": s.get("elapsed_duration_s") or 0.0,
            "training_load": s.get("training_load") or 0.0,
        }
        for s in current
    ]
    series = weekly_series(series_rows, start_iso, end_iso)

    # ── Data quality: what is missing, and what that costs ───────────────────
    no_detail = [s["activity_id"] for s in current if s["detail_status"] == "not_ingested"]
    elapsed_only = [
        s["activity_id"] for s in current if not s.get("active_time_available")
    ]
    no_lengths = [s["activity_id"] for s in current if not s["lengths_recorded"]]
    zero_distance = [s["activity_id"] for s in current if not s.get("distance_m")]
    unknown_pool = [
        s["activity_id"] for s in current
        if s.get("pool_length_m") is None and s.get("type") == "lap_swimming"
    ]

    result: dict[str, Any] = {
        "available": bool(current),
        "as_of": end.isoformat(),
        "timezone": tz,
        "analysis_window": {"days": days, "start": start_iso, "end": end_iso},
        "baseline_window": {
            "days": baseline_days, "start": base_start_iso, "end": base_end_iso,
            "selection_method": "the equally long window immediately preceding the analysis window",
        },
        "filters": {
            "pool_length_m": pool_length_m,
            "stroke": normalize_stroke(stroke) if stroke else None,
            "min_distance_m": min_distance_m,
            "excluded_by_filter": filtered_out,
        },
        "summary": current_agg,
        "baseline_summary": baseline_agg,
        "comparison": {
            "active_pace_s_per_100m": _compare_metric(
                current_agg["active_pace_s_per_100m"],
                baseline_agg["active_pace_s_per_100m"],
                lower_is_better=True, unit="seconds per 100 m",
                unavailable_reason=(
                    "active/moving swim time was not recorded in one or both windows"
                ),
            ),
            "elapsed_pace_s_per_100m": _compare_metric(
                current_agg["elapsed_pace_s_per_100m"],
                baseline_agg["elapsed_pace_s_per_100m"],
                lower_is_better=True, unit="seconds per 100 m",
            ),
            "avg_rest_duration_s": _compare_metric(
                current_agg["avg_rest_duration_s"],
                baseline_agg["avg_rest_duration_s"],
                lower_is_better=True, unit="seconds per session",
            ),
            "weekly_distance_m": _compare_metric(
                current_agg["weekly_distance_m"], baseline_agg["weekly_distance_m"],
                lower_is_better=False, unit="metres per week",
            ),
            "sessions_per_week": _compare_metric(
                current_agg["sessions_per_week"], baseline_agg["sessions_per_week"],
                lower_is_better=False, unit="sessions per week",
            ),
            "avg_hr": _compare_metric(
                current_agg["avg_hr"], baseline_agg["avg_hr"],
                lower_is_better=False, unit="bpm",
            ),
            "interpretation_note": (
                "active pace, elapsed pace and rest are compared separately on "
                "purpose: a quicker session at the same distance can come from "
                "faster swimming, shorter rests or greater effort, and only the "
                "active-pace change with heart-rate context distinguishes them"
            ),
        },
        "weekly_series": series,
        "trends": weekly_trends(series),
        "efficiency_by_pool_and_stroke": efficiency_by_comparability(current),
        "continuous_bests": window_continuous_bests(current),
        "by_modality": _by_modality(current),
        "coverage": coverage_report(db, start_iso, end_iso),
        "data_quality": {
            "sessions_without_detail_ingested": no_detail,
            "elapsed_time_only_sessions": elapsed_only,
            "sessions_without_length_data": no_lengths,
            "zero_distance_sessions": zero_distance,
            "lap_swims_without_known_pool_length": unknown_pool,
            "notes": [
                note for note in (
                    (
                        "elapsed-time-only sessions report no active pace, SWOLF, "
                        "stroke efficiency or continuous PR — those are not "
                        "derivable from a whole-session average"
                    ) if elapsed_only else None,
                    (
                        "some sessions have no per-length data; continuous-effort "
                        "bests are computed only from the sessions that do"
                    ) if no_lengths else None,
                    (
                        "run backfill_swim_details to ingest detail for sessions "
                        "recorded before detail ingestion existed"
                    ) if no_detail else None,
                ) if note
            ],
        },
        "sessions_included": len(current),
    }
    if not current:
        result["unavailable_reason"] = (
            f"no swim sessions between {start_iso} and {end_iso}"
            + (" matching the requested filters" if filtered_out else "")
        )
    if include_sessions:
        result["sessions"] = sorted(
            current, key=lambda s: s.get("day") or "", reverse=True
        )[: max(1, min(session_limit, 200))]
        result["sessions_truncated"] = len(current) > session_limit
    return result


def _by_modality(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool and open-water totals kept apart — they are not comparable."""
    out: dict[str, Any] = {}
    for session in sessions:
        key = session.get("type") or "unknown"
        entry = out.setdefault(key, {
            "session_count": 0, "total_distance_m": 0.0, "activity_ids": [],
        })
        entry["session_count"] += 1
        entry["total_distance_m"] = round(
            entry["total_distance_m"] + (session.get("distance_m") or 0.0), 1
        )
        entry["activity_ids"].append(session.get("activity_id"))
    return out
