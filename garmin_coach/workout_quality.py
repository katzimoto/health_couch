"""Workout data-quality detection — typed findings over recorded sessions.

The motivating case: a watch records a "strength training" activity that lasts
eleven seconds while a detailed manual log of the same session says 55 minutes.
Row-level reconciliation would happily let the device's duration, calories and
training load win (Garmin is the physiology authority) and the session would
read as eleven seconds of work. That is not a merge preference problem — it is a
*recording quality* problem, and the fix is to detect it and say so.

This module is the single warning pipeline: it extends the detector that
``coaching_context.detect_workout_quality_warnings`` already exposed (zero
distance, implausible speed) rather than competing with it — that function is
now a thin legacy-shaped wrapper over :func:`detect_findings`.

Findings are **typed, evidenced and actionable**:

``{type, severity, day, activity_id, related_activity_ids, evidence,
suggested_action, blocks_records}``

Nothing here mutates anything. The report is read-only by construction: it takes
plain rows and returns findings, and the MCP tool that exposes it never calls a
merge. What it *does* feed is:

* :mod:`~garmin_coach.workout_merge`, whose field resolution consults
  :func:`source_quality` so a truncated recording cannot supply the session's
  duration, calories or load; and
* the progression services, which exclude flagged sessions from records and
  report the exclusion instead of silently dropping data.
"""

from __future__ import annotations

from typing import Any

from .strength_merge import _parse_start, is_strength_like

# ── Thresholds (tune here, not at call sites) ─────────────────────────────────

# Below this, a recorded "session" is not a session: it is a mis-start, an
# accidental trigger, or a recording that stopped immediately.
NEAR_ZERO_DURATION_S = 120.0

# A device recording covering less than this fraction of a same-session manual
# log is a partial recording: its HR/calorie/load figures describe a slice, not
# the session.
PARTIAL_COVERAGE_RATIO = 0.5

# Two sources of one session whose durations differ by more than this are
# inconsistent enough to report, even when both are plausible.
SOURCE_DURATION_MISMATCH_RATIO = 0.5

# Faster than a person runs or rides casually (m/s). Cycling is exempt.
IMPLAUSIBLE_SPEED_MS = 12.5

# A distance sport that ran this long with no distance has a broken recording.
MIN_DISTANCE_SPORT_DURATION_S = 300.0

SEVERITIES = ("info", "warning", "critical")

FINDING_TYPES = (
    "near_zero_duration",
    "partial_recording",
    "zero_distance",
    "impossible_speed",
    "impossible_time_distance",
    "missing_essential_data",
    "source_field_mismatch",
    "unresolved_match_candidate",
)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _is_distance_sport(workout_type: str | None) -> bool:
    text = (workout_type or "").lower()
    return any(k in text for k in ("run", "walk", "cycl", "bike", "row", "swim"))


def _finding(
    finding_type: str,
    severity: str,
    workout: dict[str, Any],
    *,
    evidence: dict[str, Any],
    suggested_action: str,
    related: list[int] | None = None,
    blocks_records: bool = False,
) -> dict[str, Any]:
    return {
        "type": finding_type,
        "severity": severity,
        "day": workout.get("day"),
        "activity_id": workout.get("activity_id"),
        "canonical_activity_id": workout.get("duplicate_of") or workout.get("activity_id"),
        "source": workout.get("source"),
        "name": workout.get("name"),
        "workout_type": workout.get("type"),
        "related_activity_ids": sorted(related or []),
        "evidence": evidence,
        "suggested_action": suggested_action,
        "blocks_records": blocks_records,
    }


# ── Detection ─────────────────────────────────────────────────────────────────


def detect_findings(
    workouts: list[dict[str, Any]],
    *,
    include_all_sources: bool = False,
    links: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Typed quality findings over a set of workout rows.

    ``workouts`` may include duplicate/source rows (pass
    ``include_all_sources=True`` when they do) — cross-source checks need both
    sides of a merge. ``links`` are ``workout_source_link`` rows, used to tell a
    resolved pair from an unresolved candidate.
    """
    findings: list[dict[str, Any]] = []
    by_day: dict[str, list[dict[str, Any]]] = {}
    for workout in workouts:
        by_day.setdefault(workout.get("day") or "", []).append(workout)

    linked_pairs: set[frozenset[int]] = set()
    canonical_of: dict[int, int] = {}
    for link in links or []:
        source_id = link.get("source_activity_id")
        canonical_id = link.get("canonical_activity_id")
        if source_id is not None and canonical_id is not None:
            canonical_of[source_id] = canonical_id
    for source_id, canonical_id in canonical_of.items():
        for other_id, other_canonical in canonical_of.items():
            if other_id != source_id and other_canonical == canonical_id:
                linked_pairs.add(frozenset({source_id, other_id}))

    for workout in workouts:
        findings.extend(_single_row_findings(workout))

    for day_workouts in by_day.values():
        findings.extend(_cross_source_findings(day_workouts, linked_pairs, canonical_of))

    order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    return sorted(
        findings,
        key=lambda f: (order.get(f["severity"], 99), f.get("day") or "", f.get("activity_id") or 0),
    )


def _single_row_findings(workout: dict[str, Any]) -> list[dict[str, Any]]:
    """Checks that need only the row itself."""
    out: list[dict[str, Any]] = []
    duration = _num(workout.get("duration_s"))
    distance = _num(workout.get("distance_m"))
    wtype = workout.get("type")

    if (duration is None or duration <= 0) and (distance is None or distance <= 0):
        out.append(_finding(
            "missing_essential_data", "warning", workout,
            evidence={"duration_s": duration, "distance_m": distance},
            suggested_action=(
                "no duration and no distance were recorded — log the session "
                "manually or re-sync; it is excluded from volume and records"
            ),
            blocks_records=True,
        ))
        return out

    if duration is not None and 0 < duration < NEAR_ZERO_DURATION_S:
        out.append(_finding(
            "near_zero_duration", "warning", workout,
            evidence={
                "duration_s": duration,
                "threshold_s": NEAR_ZERO_DURATION_S,
            },
            suggested_action=(
                "a recording this short is very unlikely to be a whole session; "
                "treat its heart rate, calories and load as covering a slice only"
            ),
            blocks_records=True,
        ))

    if _is_distance_sport(wtype) and duration and duration > MIN_DISTANCE_SPORT_DURATION_S \
            and (distance is None or distance == 0):
        out.append(_finding(
            "zero_distance", "warning", workout,
            evidence={"duration_s": duration, "distance_m": distance},
            suggested_action="excluded_from_pace_calcs",
            blocks_records=True,
        ))

    if distance and duration and duration > 0:
        speed = distance / duration
        wtext = (wtype or "").lower()
        if speed > IMPLAUSIBLE_SPEED_MS and "cycl" not in wtext and "bike" not in wtext:
            out.append(_finding(
                "impossible_speed", "critical", workout,
                evidence={
                    "distance_m": distance, "duration_s": duration,
                    "avg_speed_kmh": round(speed * 3.6, 1),
                    "threshold_kmh": round(IMPLAUSIBLE_SPEED_MS * 3.6, 1),
                },
                suggested_action=(
                    "distance and duration are physically inconsistent — review "
                    "the record; it is excluded from pace and records"
                ),
                blocks_records=True,
            ))
    elif distance and (duration is None or duration <= 0):
        out.append(_finding(
            "impossible_time_distance", "warning", workout,
            evidence={"distance_m": distance, "duration_s": duration},
            suggested_action=(
                "a distance was recorded with no duration — no pace or speed can "
                "be derived from it"
            ),
            blocks_records=True,
        ))
    return out


def _cross_source_findings(
    day_workouts: list[dict[str, Any]],
    linked_pairs: set[frozenset[int]],
    canonical_of: dict[int, int],
) -> list[dict[str, Any]]:
    """Checks that compare two records of what may be one session."""
    out: list[dict[str, Any]] = []
    sources = [w for w in day_workouts if w.get("source") != "merged"]
    for i, first in enumerate(sources):
        for second in sources[i + 1:]:
            if not _plausibly_same_session(first, second):
                continue
            a_id, b_id = first.get("activity_id"), second.get("activity_id")
            a_dur, b_dur = _num(first.get("duration_s")), _num(second.get("duration_s"))
            pair_linked = frozenset({a_id, b_id}) in linked_pairs

            if a_dur and b_dur:
                longer, shorter = (first, second) if a_dur >= b_dur else (second, first)
                long_dur, short_dur = max(a_dur, b_dur), min(a_dur, b_dur)
                coverage = short_dur / long_dur
                if coverage < PARTIAL_COVERAGE_RATIO:
                    out.append(_finding(
                        "partial_recording", "critical", shorter,
                        evidence={
                            "recorded_duration_s": short_dur,
                            "other_source_duration_s": long_dur,
                            "coverage_ratio": round(coverage, 3),
                            "other_source": longer.get("source"),
                            "other_activity_id": longer.get("activity_id"),
                        },
                        related=[longer.get("activity_id")],
                        suggested_action=(
                            "this recording covers only "
                            f"{coverage * 100:.0f}% of the session another source "
                            "recorded — keep the longer source's duration and treat "
                            "this record's heart rate, calories and load as partial "
                            "coverage rather than whole-session physiology"
                        ),
                        blocks_records=True,
                    ))
                elif coverage < SOURCE_DURATION_MISMATCH_RATIO + 0.5 and abs(
                    a_dur - b_dur
                ) / long_dur > SOURCE_DURATION_MISMATCH_RATIO:
                    out.append(_finding(
                        "source_field_mismatch", "warning", shorter,
                        evidence={
                            "field": "duration_s",
                            "values": {
                                str(first.get("source")): a_dur,
                                str(second.get("source")): b_dur,
                            },
                        },
                        related=[longer.get("activity_id")],
                        suggested_action=(
                            "two sources disagree about this session's duration — "
                            "review before trusting derived pace or load"
                        ),
                    ))

            if not pair_linked and canonical_of.get(a_id) is None \
                    and canonical_of.get(b_id) is None:
                confidence, reason = _match_evidence(first, second)
                out.append(_finding(
                    "unresolved_match_candidate",
                    "info" if confidence < 0.5 else "warning",
                    first,
                    evidence={
                        "candidate_activity_id": b_id,
                        "match_confidence": confidence,
                        "match_evidence": reason,
                        "same_date_only": confidence < 0.5,
                    },
                    related=[b_id],
                    suggested_action=(
                        "these two records may be one session; they are left "
                        "unresolved because the available timing/duration evidence "
                        "is not sufficient — link them explicitly with "
                        "merge_workout_sources(source_activity_ids=[...]) if they are"
                    ),
                ))
    return out


def _plausibly_same_session(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two same-day rows could be one session at all.

    Requires different sources and a compatible activity type. **Being on the
    same date is never enough on its own** — that is exactly the mistake the
    issue calls out — so a pair with no other supporting evidence is reported as
    *unresolved*, never merged.
    """
    if a.get("activity_id") == b.get("activity_id"):
        return False
    if (a.get("source") or "garmin") == (b.get("source") or "garmin"):
        return False
    a_type, b_type = a.get("type"), b.get("type")
    if is_strength_like(a_type) and is_strength_like(b_type):
        return True
    return bool(a_type) and a_type == b_type


def _match_evidence(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, str]:
    """How strongly timing and duration support these being one session."""
    reasons = ["same day, compatible type"]
    confidence = 0.4
    a_start, b_start = _parse_start(a.get("start_time")), _parse_start(b.get("start_time"))
    if a_start and b_start:
        gap_h = abs((a_start - b_start).total_seconds()) / 3600.0
        if gap_h <= 1.0:
            confidence += 0.3
            reasons.append(f"start times within {gap_h:.2f}h")
        else:
            confidence -= 0.2
            reasons.append(f"start times {gap_h:.1f}h apart")
    else:
        reasons.append("no comparable start time on both records")
    a_dur, b_dur = _num(a.get("duration_s")), _num(b.get("duration_s"))
    if a_dur and b_dur:
        ratio = abs(a_dur - b_dur) / max(a_dur, b_dur)
        if ratio <= 0.2:
            confidence += 0.2
            reasons.append("durations comparable")
        else:
            confidence -= 0.1
            reasons.append(f"durations differ by {ratio * 100:.0f}%")
    return round(max(0.0, min(confidence, 1.0)), 2), "; ".join(reasons)


# ── Consumers ─────────────────────────────────────────────────────────────────


def findings_to_warnings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The legacy warning shape kept by ``detect_workout_quality_warnings``.

    Existing consumers (the daily coaching context and its tests) read
    ``{activity_id, field, status, reason, action}``; one pipeline produces
    both shapes rather than two detectors drifting apart.
    """
    field_by_type = {
        "zero_distance": "distance_m",
        "impossible_speed": "distance_m/duration_s",
        "impossible_time_distance": "distance_m/duration_s",
        "near_zero_duration": "duration_s",
        "partial_recording": "duration_s",
        "missing_essential_data": "duration_s/distance_m",
        "source_field_mismatch": "duration_s",
    }
    out: list[dict[str, Any]] = []
    for finding in findings:
        if finding["type"] not in field_by_type:
            continue
        out.append({
            "activity_id": finding["activity_id"],
            "field": field_by_type[finding["type"]],
            "status": "suspicious",
            "reason": _legacy_reason(finding),
            "action": (
                "excluded_from_pace_calcs"
                if finding["type"] == "zero_distance" else "flag_for_review"
            ),
        })
    return out


def _legacy_reason(finding: dict[str, Any]) -> str:
    evidence = finding["evidence"]
    if finding["type"] == "zero_distance":
        wtype = (finding.get("workout_type") or "distance").lower()
        return (
            f"{wtype} activity of {evidence['duration_s'] / 60:.0f} min has "
            "zero/no distance"
        )
    if finding["type"] == "impossible_speed":
        return f"implausible average speed {evidence['avg_speed_kmh']:.0f} km/h"
    if finding["type"] == "near_zero_duration":
        return (
            f"recording lasted only {evidence['duration_s']:.0f}s — unlikely to "
            "be a whole session"
        )
    if finding["type"] == "partial_recording":
        return (
            f"recording covers {evidence['coverage_ratio'] * 100:.0f}% of the "
            f"{evidence['other_source']} record of the same session"
        )
    return finding["suggested_action"]


def record_blocking_ids(findings: list[dict[str, Any]]) -> set[int]:
    """Activity ids whose data must not create records or headline totals."""
    return {
        f["activity_id"] for f in findings
        if f.get("blocks_records") and f.get("activity_id") is not None
    }


def source_quality(sources: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-source completeness used by quality-aware field selection.

    A source whose recorded duration covers less than
    :data:`PARTIAL_COVERAGE_RATIO` of the longest duration among the sources of
    the same session is *incomplete*: its whole-session fields (duration,
    calories, training load) must not win merely because its source normally
    has priority, and its heart-rate figures describe only the slice it covers.
    """
    durations = {
        name: _num(row.get("duration_s"))
        for name, row in sources.items()
    }
    usable = [d for d in durations.values() if d and d > 0]
    longest = max(usable) if usable else None
    out: dict[str, dict[str, Any]] = {}
    for name, duration in durations.items():
        if longest is None or duration is None or duration <= 0:
            out[name] = {
                "duration_s": duration,
                "coverage_ratio": None,
                "complete": duration is not None and duration > 0,
                "reason": None if duration else "no duration recorded",
            }
            continue
        ratio = duration / longest
        complete = ratio >= PARTIAL_COVERAGE_RATIO
        out[name] = {
            "duration_s": duration,
            "coverage_ratio": round(ratio, 3),
            "complete": complete,
            "reason": None if complete else (
                f"covers {ratio * 100:.0f}% of the longest recorded duration for "
                "this session"
            ),
        }
    return out


def quality_report(
    workouts: list[dict[str, Any]],
    links: list[dict[str, Any]] | None = None,
    *,
    days: int,
) -> dict[str, Any]:
    """The read-only report body: findings plus their roll-up."""
    findings = detect_findings(workouts, include_all_sources=True, links=links)
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for finding in findings:
        by_type[finding["type"]] = by_type.get(finding["type"], 0) + 1
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1
    return {
        "window_days": days,
        "workouts_examined": len(workouts),
        "findings": findings,
        "finding_count": len(findings),
        "by_type": by_type,
        "by_severity": by_severity,
        "records_blocked_activity_ids": sorted(record_blocking_ids(findings)),
        "read_only": True,
        "note": (
            "detection only — nothing was modified. Preview a fix with "
            "merge_workout_sources(..., dry_run=True) and apply it explicitly."
        ),
        "thresholds": {
            "near_zero_duration_s": NEAR_ZERO_DURATION_S,
            "partial_coverage_ratio": PARTIAL_COVERAGE_RATIO,
            "implausible_speed_kmh": round(IMPLAUSIBLE_SPEED_MS * 3.6, 1),
        },
    }
