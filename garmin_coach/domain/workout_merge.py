"""Field-level merge of a strength session recorded by more than one source.

Row-level dedupe (``dedupe_workouts``) keeps exactly one source and hides the
rest — fine for a walk recorded twice, wrong for strength. Garmin is accurate
for *physiology* (HR, calories, duration, training load, timing) but records a
lift as a generic "strength" activity with no exercise detail; the manual log
is accurate for *exercise details* (names/order/sets/reps/weights/RPE/notes)
but its HR/calorie/load figures are estimates. The right answer keeps both and
builds a canonical workout whose every physical field remembers which source
it came from.

This module is pure (no DB access): the manual↔Garmin matcher and the
per-domain field-priority resolver are unit-testable on their own. The
:class:`~garmin_coach.database.Database` merge service wires them to rows.
"""

from __future__ import annotations

from typing import Any

from garmin_coach.domain.strength_merge import _parse_start, is_strength_like

# ── Field-level (domain) source priority ────────────────────────────────────────
# The spec's key rule: priority is per *domain*, never one global row winner.
# "manual_estimated"/"apple_estimated" in the brief just mean "that source's
# estimated load" — every non-Garmin load already carries load_source, so the
# row-source order below is enough and the winning row's load_source is kept.
_DOMAIN_PRIORITY: dict[str, tuple[str, ...]] = {
    "duration": ("garmin", "apple", "manual"),
    "physiology": ("garmin", "apple", "manual"),
    "calories": ("garmin", "apple", "manual"),
    "training_load": ("garmin", "manual", "apple"),
}

# The physical Workout columns each domain owns.
_DOMAIN_COLUMNS: dict[str, tuple[str, ...]] = {
    "duration": ("duration_s",),
    "physiology": ("avg_hr", "max_hr", "start_time", "distance_m"),
    "calories": ("calories",),
    "training_load": ("training_load",),
}

# name/type: manual wins for a strength session, Garmin for pure cardio —
# unless the higher-priority source simply doesn't carry the field.
_STRENGTH_NAME_PRIORITY = ("manual", "apple", "garmin")
_CARDIO_NAME_PRIORITY = ("garmin", "apple", "manual")

# Matcher tolerances (tune here, not at call sites).
MATCH_MAX_START_GAP_H = 3.0
MATCH_MAX_DURATION_RATIO = 0.5
DEFAULT_MIN_CONFIDENCE = 0.5


def normalize_source(row: dict[str, Any]) -> str:
    """The source bucket a workout row belongs to for field priority.

    A ``garmin_merged`` canonical (several Garmin fragments already folded into
    one) is still Garmin physiology, so it competes as ``garmin``.
    """
    source = row.get("source")
    if source in (None, ""):
        return "garmin" if (row.get("activity_id") or 0) > 0 else "manual"
    if source == "garmin_merged":
        return "garmin"
    return source


def strength_match(
    manual: dict[str, Any],
    garmin: dict[str, Any],
    max_start_gap_h: float = MATCH_MAX_START_GAP_H,
    max_duration_ratio: float = MATCH_MAX_DURATION_RATIO,
) -> tuple[float, str] | None:
    """Score a manual strength session against a Garmin activity as the same
    physical workout, or ``None`` if they're incompatible.

    Both must be strength-like and same-day (callers group by day). Timing and
    duration, *when present on both*, tighten or reject the match; when the
    manual log has no start time we fall back to a date-only match at lower
    confidence rather than guessing. Confidence is in ``[0, 1]``.
    """
    if not is_strength_like(manual.get("type")) or not is_strength_like(garmin.get("type")):
        return None

    reasons = ["same-day strength type"]
    confidence = 0.5

    m_start = _parse_start(manual.get("start_time"))
    g_start = _parse_start(garmin.get("start_time"))
    if m_start is not None and g_start is not None:
        gap_h = abs((m_start - g_start).total_seconds()) / 3600.0
        if gap_h > max_start_gap_h:
            return None
        confidence += 0.3 * (1.0 - gap_h / max_start_gap_h)
        reasons.append(f"start times within {gap_h:.1f}h")
    else:
        reasons.append("no manual start time — matched by date only")

    m_dur, g_dur = manual.get("duration_s"), garmin.get("duration_s")
    if m_dur and g_dur:
        ratio = abs(m_dur - g_dur) / max(m_dur, g_dur)
        if ratio > max_duration_ratio:
            return None
        confidence += 0.2 * (1.0 - ratio / max_duration_ratio)
        reasons.append("durations comparable")

    return round(min(confidence, 1.0), 2), "; ".join(reasons)


def best_strength_match(
    manual: dict[str, Any],
    garmin_candidates: list[dict[str, Any]],
    max_start_gap_h: float = MATCH_MAX_START_GAP_H,
    max_duration_ratio: float = MATCH_MAX_DURATION_RATIO,
) -> tuple[dict[str, Any], float, str] | None:
    """The best-scoring Garmin candidate for one manual session, or ``None``.

    Ties (same confidence — e.g. two untimed candidates) break to the nearest
    duration, then the newest activity_id, so the choice is deterministic.
    """
    scored: list[tuple[float, float, int, dict[str, Any], str]] = []
    m_dur = manual.get("duration_s") or 0
    for garmin in garmin_candidates:
        result = strength_match(manual, garmin, max_start_gap_h, max_duration_ratio)
        if result is None:
            continue
        confidence, reason = result
        dur_gap = abs((garmin.get("duration_s") or 0) - m_dur)
        scored.append((confidence, -dur_gap, garmin.get("activity_id") or 0, garmin, reason))
    if not scored:
        return None
    confidence, _neg_gap, _aid, garmin, reason = max(
        scored, key=lambda t: (t[0], t[1], t[2])
    )
    return garmin, confidence, reason


def merge_fields(
    sources: dict[str, dict[str, Any]], is_strength: bool
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve the canonical physical fields from per-source rows.

    ``sources`` maps a normalized source name (``manual``/``garmin``/``apple``)
    to that source's workout row. Returns ``(merged_columns, field_sources)``
    where ``field_sources`` records, per field, which source won — the
    provenance the report and ``get_merged_workout`` surface.
    """
    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}

    for domain, columns in _DOMAIN_COLUMNS.items():
        priority = _DOMAIN_PRIORITY[domain]
        for column in columns:
            for src in priority:
                row = sources.get(src)
                if row is not None and row.get(column) is not None:
                    merged[column] = row[column]
                    provenance[column] = src
                    break

    # training_load keeps the winning source's load provenance: a real Garmin
    # load stays "garmin"; anything else is an estimate. This is exactly the
    # "manual estimated load is not used if Garmin load exists" rule.
    if "training_load" in provenance:
        tl_src = provenance["training_load"]
        tl_row = sources[tl_src]
        merged["load_source"] = (
            "garmin" if tl_src == "garmin" and tl_row.get("load_source") == "garmin"
            else "estimated"
        )

    name_priority = _STRENGTH_NAME_PRIORITY if is_strength else _CARDIO_NAME_PRIORITY
    for field in ("name", "type"):
        for src in name_priority:
            row = sources.get(src)
            if row is not None and row.get(field) is not None:
                merged[field] = row[field]
                provenance[field] = src
                break

    # Exercise details are never a Workout column — they live in the strength
    # session — but their provenance belongs in field_sources so a reader knows
    # the exercise log came from the manual source.
    if is_strength and "manual" in sources:
        provenance["exercise_details"] = "manual"

    return merged, provenance


def fields_from_source(provenance: dict[str, str], source: str) -> list[str]:
    """Which canonical fields a given source provided (for a link's audit)."""
    return sorted(field for field, src in provenance.items() if src == source)
