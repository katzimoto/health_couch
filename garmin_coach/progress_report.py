"""The unified, baseline-relative training progress report.

``get_training_progress_report(days=56)`` is the default answer source for
*"how am I progressing?"*. It composes the deterministic services built for the
individual sports rather than asking a language model to do arithmetic over raw
logs, and it is deliberately **complete**: every activity type actually recorded
in the window gets a section, so swimming (or rowing, or anything else) cannot
be silently omitted because nobody asked about it by name.

Structure of the answer:

``sports`` · ``strength`` · ``adherence`` · ``recovery`` · ``notable_prs`` ·
``concerns`` · ``data_quality``

Each section carries an availability marker and, when unavailable, a reason.
The rules the tests pin:

* **One scan, shared primitives.** The sports section buckets a single
  ``recent_workouts`` read through the same
  :mod:`~garmin_coach.activity_progress` aggregation the per-sport tool uses, so
  its totals equal those of ``get_activity_progress`` for the same window and
  cannot drift. Detail is opt-in.
* **The user is compared with their own documented baseline** — the equally long
  window immediately before the analysis window — and the selection method,
  coverage and exclusions are stated. No population rankings, no forecasts.
* **Observations, estimates and interpretation are labelled separately.** A
  faster pace at a higher heart rate is an observation; an estimated 1RM is an
  estimate; "you are fitter" is an interpretation this report does not make.
* **Adherence has an explicit denominator.** No recorded plan means adherence is
  *unknown*, never 0%.
* **Recovery is reused, not re-implemented**, and is presented as context — the
  report does not claim it caused a performance change.
* **No blended "overall fitness +X%".** Unrelated lifts and sports are never
  collapsed into one unexplained number.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .activity_progress import (
    MAX_WINDOW_DAYS,
    activity_family,
    compare,
    coverage_report,
    metric_profile,
    normalize_activity_type,
    observed_records,
    summarize_sessions,
    today_in,
    window_bounds,
)

# Bound the report: a user with a long tail of one-off activity types should
# still get a bounded payload, with the tail reported rather than hidden.
MAX_SPORT_SECTIONS = 12
MAX_STRENGTH_EXERCISES = 12
MAX_PRS = 12


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


# ── Adherence ─────────────────────────────────────────────────────────────────


def adherence_summary(plans: list[dict[str, Any]]) -> dict[str, Any]:
    """Planned-vs-actual from ``TrainingPlan`` statuses.

    The denominator is the number of plans actually recorded in the window, and
    it is reported. With no recorded plan there is nothing to adhere to, so the
    answer is *unknown* — never zero, which would read as "skipped everything".
    """
    if not plans:
        return {
            "available": False,
            "unavailable_reason": (
                "no training plan was recorded in this window — adherence is "
                "unknown, not zero"
            ),
            "planned_sessions": 0,
        }
    counts = {"done": 0, "partially_done": 0, "skipped": 0, "planned": 0, "other": 0}
    for plan in plans:
        status = (plan.get("status") or "planned").lower()
        counts[status if status in counts else "other"] += 1

    resolved = counts["done"] + counts["partially_done"] + counts["skipped"]
    total = len(plans)
    completed_rate = (
        round((counts["done"] + 0.5 * counts["partially_done"]) / resolved * 100, 1)
        if resolved else None
    )
    return {
        "available": True,
        "planned_sessions": total,
        "denominator": resolved,
        "denominator_note": (
            "plans still marked 'planned' (future or never updated) are excluded "
            "from the rate and reported separately"
        ),
        "completed": counts["done"],
        "partially_completed": counts["partially_done"],
        "skipped": counts["skipped"],
        "still_open": counts["planned"],
        "other_status": counts["other"],
        "completion_rate_pct": completed_rate,
        "rate_method": (
            "(completed + 0.5 × partially completed) ÷ resolved plans; a partial "
            "session is counted as half rather than as a completion or a skip"
        ),
        "skip_reasons": [
            {"day": p.get("day"), "plan_id": p.get("id"), "reason": p.get("skip_reason")}
            for p in plans
            if (p.get("status") or "").lower() == "skipped" and p.get("skip_reason")
        ][:10],
        **({"rate_unavailable_reason": "no plan in the window has been resolved yet"}
           if completed_rate is None else {}),
    }


# ── Recovery context (reused, not re-implemented) ─────────────────────────────


def recovery_context(report: dict[str, Any], readiness: dict[str, Any] | None) -> dict[str, Any]:
    """Sleep / HRV / resting-HR / subjective readiness, from the analyzer.

    Deliberately a *context* block: it is reported beside the performance
    numbers, never used to assert that recovery caused a change in them.
    """
    if not report.get("available"):
        return {
            "available": False,
            "unavailable_reason": report.get("reason", "no daily data yet"),
        }
    trends = report.get("trends", {}) or {}
    return {
        "available": True,
        "as_of": report.get("as_of"),
        "source": "Analyzer.report — the same recovery computation the daily coaching context uses",
        "sleep_hours": trends.get("sleep_hours"),
        "sleep_score": trends.get("sleep_score"),
        "sleep_debt_7d_estimate": report.get("sleep_debt_7d"),
        "sleep_target_hours": report.get("sleep_target_hours"),
        "hrv": trends.get("hrv"),
        "resting_hr": trends.get("resting_hr"),
        "subjective_readiness": readiness,
        "flags": report.get("flags", []),
        "interpretation_note": (
            "recovery is reported as context alongside performance, not as a "
            "cause of it — this report does not claim one explains the other"
        ),
    }


# ── Sports ────────────────────────────────────────────────────────────────────


def sport_sections(
    workouts: list[dict[str, Any]],
    *,
    start: str,
    end: str,
    baseline_start: str,
    baseline_end: str,
    window_days: int,
    baseline_days: int,
    excluded_ids: set[int],
) -> tuple[list[dict[str, Any]], bool]:
    """One section per activity type recorded in the analysis window.

    Built from the *shared* aggregation primitives over a single scan, so these
    totals are the same numbers ``get_activity_progress`` reports for the same
    window — there is no second implementation to drift.
    """
    current_rows = [w for w in workouts if start <= (w.get("day") or "") <= end]
    baseline_rows = [
        w for w in workouts if baseline_start <= (w.get("day") or "") <= baseline_end
    ]

    def bucket(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(normalize_activity_type(row.get("type")) or "unknown", []).append(row)
        return out

    current_buckets = bucket(current_rows)
    baseline_buckets = bucket(baseline_rows)

    ordered = sorted(
        current_buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])
    )
    truncated = len(ordered) > MAX_SPORT_SECTIONS
    sections: list[dict[str, Any]] = []
    for activity_type, rows in ordered[:MAX_SPORT_SECTIONS]:
        current = summarize_sessions(
            rows, activity_type, window_days=window_days,
            excluded_activity_ids=excluded_ids,
        )
        baseline = summarize_sessions(
            baseline_buckets.get(activity_type, []), activity_type,
            window_days=baseline_days, excluded_activity_ids=excluded_ids,
        )
        profile = metric_profile(activity_type)
        section = {
            "activity_type": activity_type,
            "family": profile["family"],
            "available": True,
            "summary": current,
            "baseline_summary": baseline,
            "comparison": compare(current, baseline, activity_type),
            "records": observed_records(rows, activity_type, excluded_ids),
            "activity_ids": sorted(
                r["activity_id"] for r in rows if r.get("activity_id") is not None
            )[:50],
            "specialist_tool": profile["delegates_to"],
        }
        if not baseline_buckets.get(activity_type):
            section["baseline_note"] = (
                "no sessions of this type in the baseline window — changes are "
                "reported as unavailable rather than as growth from zero"
            )
        sections.append(section)
    return sections, truncated


# ── PRs and concerns ──────────────────────────────────────────────────────────


def collect_prs(
    sports: list[dict[str, Any]],
    strength: dict[str, Any],
    swimming: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Supported records only, each labelled with its basis and source ids.

    A whole-session observation and a continuous-effort best are different
    claims, and an estimated 1RM is neither — so each entry says what kind of
    statement it is.
    """
    out: list[dict[str, Any]] = []
    for section in sports:
        records = section.get("records") or {}
        pace = records.get("best_session_pace")
        if pace and pace.get("seconds_per_unit") is not None:
            out.append({
                "kind": "best_session_pace",
                "claim_type": "observation",
                "activity_type": section["activity_type"],
                "value": pace["seconds_per_unit"],
                "unit": pace["unit"],
                "display": pace.get("display"),
                "day": pace.get("day"),
                "activity_id": pace.get("activity_id"),
                "basis": "whole-session average, not a continuous-effort record",
            })
        longest = records.get("longest_distance_m")
        if longest and longest.get("value"):
            out.append({
                "kind": "longest_distance",
                "claim_type": "observation",
                "activity_type": section["activity_type"],
                "value": longest["value"],
                "unit": "m",
                "day": longest.get("day"),
                "activity_id": longest.get("activity_id"),
                "basis": "longest single session in the window",
            })

    for effort_key, effort in ((swimming or {}).get("continuous_bests", {}).get("efforts", {}) or {}).items():
        out.append({
            "kind": f"continuous_{effort_key}",
            "claim_type": "observation",
            "activity_type": "lap_swimming",
            "value": effort.get("duration_s"),
            "unit": "s",
            "pace_s_per_100m": effort.get("pace_s_per_100m"),
            "day": effort.get("day"),
            "activity_id": effort.get("activity_id"),
            "basis": effort.get("basis"),
        })

    for exercise in (strength.get("exercises") or []):
        heaviest = exercise.get("heaviest_weight_kg")
        if heaviest and heaviest.get("value") is not None:
            out.append({
                "kind": "heaviest_weight",
                "claim_type": "observation",
                "exercise": exercise["exercise"],
                "value": heaviest["value"],
                "unit": "kg",
                "load_convention": exercise.get("load_convention"),
                "scope": heaviest.get("scope"),
                "day": heaviest.get("date"),
                "session_id": heaviest.get("session_id"),
                "basis": "heaviest completed set logged in the window",
            })
        e1rm = exercise.get("best_estimated_1rm")
        if e1rm and e1rm.get("value_kg") is not None:
            out.append({
                "kind": "estimated_1rm",
                "claim_type": "estimate",
                "exercise": exercise["exercise"],
                "value": e1rm["value_kg"],
                "unit": "kg",
                "formula": e1rm.get("formula"),
                "day": e1rm.get("date"),
                "session_id": e1rm.get("session_id"),
                "basis": "estimated from a submaximal set — not a measured maximum",
            })
    return out[:MAX_PRS]


def collect_concerns(
    sports: list[dict[str, Any]],
    adherence: dict[str, Any],
    quality: dict[str, Any],
    coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    """What the data does *not* support, alongside the positive changes.

    Declines, effort-confounded improvements, incomplete recordings, unresolved
    reconciliation and thin coverage all belong in the same answer as the
    progress — otherwise the report only ever tells a flattering story.
    """
    concerns: list[dict[str, Any]] = []

    for section in sports:
        comparison = section.get("comparison") or {}
        pace = comparison.get("pace")
        hr = comparison.get("avg_hr") or {}
        if pace and pace.get("improved") and (hr.get("absolute_change") or 0) > 3:
            concerns.append({
                "type": "effort_confounded_improvement",
                "activity_type": section["activity_type"],
                "detail": (
                    f"pace improved by {abs(pace['absolute_change_s']):.0f}s per "
                    f"{pace['unit'].replace('min_per_', '')} but average heart rate "
                    f"rose {hr['absolute_change']:.0f} bpm — a harder effort, not "
                    "demonstrated aerobic improvement"
                ),
            })
        volume = comparison.get("weekly_duration_s") or {}
        if (volume.get("percent_change") or 0) < -25:
            concerns.append({
                "type": "volume_decline",
                "activity_type": section["activity_type"],
                "detail": (
                    f"weekly {section['activity_type']} time is "
                    f"{abs(volume['percent_change']):.0f}% below the baseline window"
                ),
            })
        if section["summary"]["workout_count"] < 3:
            concerns.append({
                "type": "thin_sample",
                "activity_type": section["activity_type"],
                "detail": (
                    f"only {section['summary']['workout_count']} session(s) in the "
                    "window — comparisons for this sport are uncertain"
                ),
            })

    if not adherence.get("available"):
        concerns.append({
            "type": "adherence_unknown",
            "detail": adherence.get("unavailable_reason"),
        })

    by_type = quality.get("by_type") or {}
    for finding_type, label in (
        ("partial_recording", "incomplete device recordings"),
        ("near_zero_duration", "near-zero-duration recordings"),
        ("impossible_speed", "physically inconsistent recordings"),
        ("unresolved_match_candidate", "unresolved possible duplicate sessions"),
    ):
        if by_type.get(finding_type):
            concerns.append({
                "type": finding_type,
                "count": by_type[finding_type],
                "detail": (
                    f"{by_type[finding_type]} {label} in the window — see "
                    "get_workout_data_quality; affected sessions are excluded "
                    "from totals and records"
                ),
            })

    if (coverage.get("coverage_ratio") or 1) < 0.8:
        concerns.append({
            "type": "sparse_sync_coverage",
            "detail": (
                f"{coverage.get('unsynced_days')} of {coverage.get('window_days')} "
                "days have no recorded sync — those days are unknown, not rest, "
                "so volume may be understated"
            ),
        })
    return concerns


# ── The service ───────────────────────────────────────────────────────────────


def build_training_progress_report(
    db,
    days: int = 56,
    *,
    baseline_days: int | None = None,
    detail: bool = False,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """The combined progress report over ``days``.

    ``detail=False`` (the default) returns the bounded summary; ``detail=True``
    adds the full per-sport progression reports and the per-exercise strength
    history for drill-down.
    """
    from .analysis import Analyzer
    from .config import settings
    from .strength_progress import build_strength_progress
    from .swimming import SWIM_TYPES, build_swimming_progress
    from .workout_quality import detect_findings, quality_report, record_blocking_ids

    tz = timezone_name or settings.timezone
    days = max(1, min(int(days), MAX_WINDOW_DAYS))
    baseline_days = max(1, min(int(baseline_days or days), MAX_WINDOW_DAYS))
    end = today_in(tz)
    start_iso, end_iso = window_bounds(days, end)
    base_end = date.fromisoformat(start_iso) - timedelta(days=1)
    base_start_iso, base_end_iso = window_bounds(baseline_days, base_end)

    # One scan covers both windows for every sport.
    span_days = (end - date.fromisoformat(base_start_iso)).days + 1
    workouts = db.recent_workouts(days=min(span_days, MAX_WINDOW_DAYS))
    window_rows = [w for w in workouts if start_iso <= (w.get("day") or "") <= end_iso]

    findings = detect_findings(window_rows)
    excluded = record_blocking_ids(findings)

    sports, sports_truncated = sport_sections(
        workouts,
        start=start_iso, end=end_iso,
        baseline_start=base_start_iso, baseline_end=base_end_iso,
        window_days=days, baseline_days=baseline_days,
        excluded_ids=excluded,
    )

    strength = build_strength_progress(
        db, days=days, include_sessions=False, max_exercises=MAX_STRENGTH_EXERCISES
    )
    has_swim = any(
        (normalize_activity_type(w.get("type")) or "") in SWIM_TYPES
        or "swim" in (normalize_activity_type(w.get("type")) or "")
        for w in window_rows
    )
    swimming = (
        build_swimming_progress(db, days=days, baseline_days=baseline_days)
        if has_swim else None
    )

    analyzer_report = Analyzer(db).report()
    recovery = recovery_context(analyzer_report, db.latest_readiness())
    plans = [
        p for p in db.get_training_plans(days=days)
        if start_iso <= (p.get("day") or "") <= end_iso
    ]
    adherence = adherence_summary(plans)
    coverage = coverage_report(db, start_iso, end_iso)
    quality = quality_report(window_rows, None, days=days)

    result: dict[str, Any] = {
        "as_of": end.isoformat(),
        "timezone": tz,
        "analysis_window": {
            "days": days, "start": start_iso, "end": end_iso,
            "boundary": "calendar days in the user timezone, today inclusive",
        },
        "comparison_window": {
            "days": baseline_days, "start": base_start_iso, "end": base_end_iso,
            "selection_method": (
                "the equally long window immediately preceding the analysis "
                "window — the user's own documented baseline, never a population "
                "reference"
            ),
        },
        "coverage": coverage,
        "sports": {
            "available": bool(sports),
            "sections": sports,
            "activity_types_covered": [s["activity_type"] for s in sports],
            "truncated": sports_truncated,
            "note": (
                "every activity type recorded in the window gets a section, so a "
                "sport is never omitted because it was not asked about by name"
            ),
            **({"unavailable_reason": "no workouts recorded in the window"}
               if not sports else {}),
        },
        "swimming": swimming if swimming else {
            "available": False,
            "unavailable_reason": "no swim sessions recorded in the window",
        },
        "strength": strength,
        "adherence": adherence,
        "recovery": recovery,
        "notable_prs": collect_prs(sports, strength, swimming),
        "concerns": collect_concerns(sports, adherence, quality, coverage),
        "data_quality": {
            **quality,
            "excluded_from_totals_and_records": sorted(excluded),
        },
        "interpretation_rules": [
            "observations, estimates and interpretation are labelled separately",
            "comparisons are against the user's own preceding window, with its "
            "length and coverage stated",
            "no blended 'overall fitness' number is produced: unrelated lifts and "
            "sports are not collapsed into one figure",
            "recovery is context, not a causal explanation of performance change",
        ],
        "detail_included": detail,
    }

    if detail:
        from .activity_progress import build_activity_progress
        from .sport_load import build_sport_training_load

        result["sport_detail"] = {
            section["activity_type"]: build_activity_progress(
                db, section["activity_type"], days=days, baseline_days=baseline_days
            )
            for section in sports
        }
        result["strength_detail"] = {
            exercise["exercise"]: build_strength_progress(
                db, exercise=exercise["exercise"], days=days
            )
            for exercise in (strength.get("exercises") or [])[:MAX_STRENGTH_EXERCISES]
        }
        result["training_load_by_sport"] = build_sport_training_load(db, days=days)
    return result


def progress_report_summary(report: dict[str, Any]) -> dict[str, Any]:
    """A compact form of the report for prompts and the coaching context.

    Small enough to sit inside the daily coaching payload without dominating it,
    and carrying the same caveats — so the coach reads a bounded, labelled
    summary instead of re-deriving progress from raw rows.
    """
    return {
        "analysis_window": report.get("analysis_window"),
        "comparison_window": report.get("comparison_window"),
        "sports": [
            {
                "activity_type": section["activity_type"],
                "sessions": section["summary"]["workout_count"],
                "sessions_per_week": section["summary"]["sessions_per_week"],
                "weekly_distance_m": section["summary"].get("weekly_distance_m"),
                "weekly_duration_s": section["summary"].get("weekly_duration_s"),
                "pace": section["summary"].get("pace"),
                "pace_change": (section.get("comparison") or {}).get("pace"),
            }
            for section in (report.get("sports", {}).get("sections") or [])
        ],
        "strength": [
            {
                "exercise": exercise["exercise"],
                "sessions": exercise["sessions"],
                "top_set_weight_change": exercise.get("top_set_weight_change"),
            }
            for exercise in (report.get("strength", {}).get("exercises") or [])[:6]
        ],
        "adherence": {
            k: report.get("adherence", {}).get(k)
            for k in ("available", "planned_sessions", "completed", "skipped",
                      "completion_rate_pct", "unavailable_reason")
            if report.get("adherence", {}).get(k) is not None
        },
        "notable_prs": (report.get("notable_prs") or [])[:5],
        "concerns": (report.get("concerns") or [])[:5],
        "interpretation_rules": report.get("interpretation_rules"),
        "source": "get_training_progress_report",
    }
