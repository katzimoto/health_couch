"""Detection logic for ``merge_garmin_strength_fragments``.

Garmin sometimes splits one gym session into several short same-day
"strength" activities (pauses between exercises get recorded as separate
activities on some watch/firmware combos). This inflates workout counts and
training load. These are pure functions over already-fetched workout rows —
no database access — so the grouping heuristic is unit-testable on its own.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

# Garmin's typeKey stays in English regardless of UI locale, but be
# defensive: match the known keys and fall back to a substring check for any
# other "strength"-flavoured label a different locale/device might send.
_STRENGTH_TYPES = {
    "strength_training", "traditional_strength_training",
    "functional_strength_training", "strength",
}

# A realistic single gym visit rarely has a pause longer than this between
# recorded fragments; a bigger gap is treated as two separate sessions on the
# same day. Tune this constant, not call sites.
DEFAULT_MAX_GAP_MINUTES = 90.0
DEFAULT_MIN_FRAGMENTS = 2


def is_strength_like(workout_type: str | None) -> bool:
    if not workout_type:
        return False
    t = workout_type.strip().lower()
    return t in _STRENGTH_TYPES or "strength" in t


def _parse_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def group_fragments(
    fragments: list[dict[str, Any]],
    max_gap_minutes: float = DEFAULT_MAX_GAP_MINUTES,
) -> list[list[dict[str, Any]]]:
    """Group same-day fragments that likely belong to one gym session.

    Fragments with a parseable start time are ordered by it and split
    wherever the gap from one fragment's end to the next's start exceeds
    ``max_gap_minutes``. A fragment with no parseable start time can't be
    placed relative to the others at all — rather than guess, it's kept on
    its own (a singleton "group" too small to merge on its own), so one
    untimed fragment can never force two genuinely separate sessions to be
    merged into one.
    """
    if not fragments:
        return []
    parsed = [(_parse_start(f.get("start_time")), f) for f in fragments]
    dated = sorted((pair for pair in parsed if pair[0] is not None), key=lambda pair: pair[0])
    undated = [frag for start, frag in parsed if start is None]

    if not dated:
        return [[frag] for frag in undated]

    groups: list[list[dict[str, Any]]] = [[dated[0][1]]]
    prev_start, prev_frag = dated[0]
    for start, frag in dated[1:]:
        prev_end = prev_start + timedelta(seconds=prev_frag.get("duration_s") or 0)
        gap_minutes = (start - prev_end).total_seconds() / 60.0
        if gap_minutes <= max_gap_minutes:
            groups[-1].append(frag)
        else:
            groups.append([frag])
        prev_start, prev_frag = start, frag
    groups.extend([frag] for frag in undated)
    return groups


def weighted_avg_hr(fragments: list[dict[str, Any]]) -> int | None:
    """Average HR weighted by each fragment's duration."""
    pairs = [
        (f["avg_hr"], f.get("duration_s") or 0)
        for f in fragments if f.get("avg_hr") is not None
    ]
    total_duration = sum(d for _, d in pairs)
    if not pairs or total_duration <= 0:
        return None
    return round(sum(hr * d for hr, d in pairs) / total_duration)
