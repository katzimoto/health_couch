"""Read-time numeric normalization for strength-exercise data.

Strength rows arrive from ChatGPT tool calls, manual edits, and legacy
imports, and SQLModel ``table=True`` models skip pydantic validation on
insert — so SQLite (itself dynamically typed) happily stores ``"3"``,
``"12.5"``, a rep range like ``"10-12"``, or a JSON list where the schema
means a number. Any calculation that trusts the column types then dies with
``can't multiply sequence by non-int of type 'float'``.

This module is the single place that turns whatever shape a value was stored
in into something arithmetic-safe. Both ``get_exercise_history`` and
``recommend_next_weights`` (and the write-time aggregate derivation) go
through it:

* ``parse_float`` / ``parse_int`` — scalars, numeric strings, ``None``/empty
  → ``None``; clearly invalid values become ``None``, never a guessed number.
* ``parse_rep_range`` — ``"8-10"`` → ``(8, 10)``; a range is *bounds*, never
  a rep count to multiply.
* ``parse_reps`` — actual rep counts from a scalar, a list, or a legacy
  JSON-encoded list; a range string yields ``[]`` (it isn't a count).
* ``normalize_performance`` — one exercise record → a
  :class:`NormalizedExercisePerformance` with volume/best-set math done only
  over valid numbers. Per-set ``actual_sets`` data takes precedence over the
  aggregate columns.

Every value that was present but unusable is logged via ``log_malformed_value``
(endpoint, exercise, session/row ids, field, raw value) and normalized to
``None`` so one bad field never fails a whole endpoint.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-|–|—|to)\s*(\d+)\s*$", re.IGNORECASE)


def parse_float(value: Any) -> float | None:
    """``value`` as a finite float, or None when it can't be read as one.

    Accepts ints, floats, and numeric strings; rejects (→ None) booleans,
    empty/blank strings, NaN/inf, and anything non-numeric. Never guesses:
    ``"10-12"`` and ``"unknown"`` are None, not a number.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            result = float(text)
        except ValueError:
            return None
        return result if math.isfinite(result) else None
    return None


def parse_int(value: Any) -> int | None:
    """``value`` as an int, or None. Integral floats/strings (``3.0``,
    ``"3"``) count; fractional values don't get silently truncated."""
    number = parse_float(value)
    if number is None or number != int(number):
        return None
    return int(number)


def parse_rep_range(value: Any) -> tuple[int | None, int | None]:
    """Planned-rep bounds ``(low, high)`` from ``"8-10"``-style ranges.

    A single number (``10`` or ``"10"``) is a degenerate range ``(10, 10)``.
    Anything unreadable is ``(None, None)``. The bounds are for progression
    thresholds — a range must never be multiplied as if it were a rep count.
    """
    single = parse_int(value)
    if single is not None:
        return single, single
    if isinstance(value, str):
        match = _RANGE_RE.match(value)
        if match:
            low, high = sorted((int(match.group(1)), int(match.group(2))))
            return low, high
    return None, None


def parse_reps(value: Any) -> list[int]:
    """Actual per-set rep counts from whatever shape ``reps`` was stored in:
    a scalar (``10`` / ``"10"``) → ``[10]``, a legacy list (``[10, 10, 9]`` or
    ``["10", "10", "9"]``, possibly JSON-encoded as a string) → one entry per
    set, unreadable entries dropped. A range string like ``"10-12"`` is a
    *plan*, not a count, and yields ``[]``.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                return []
    if isinstance(value, (list, tuple)):
        parsed = (parse_int(entry) for entry in value)
        return [reps for reps in parsed if reps is not None]
    single = parse_int(value)
    return [single] if single is not None else []


def log_malformed_value(
    endpoint: str,
    exercise_name: str | None,
    session_id: Any,
    row_id: Any,
    field_name: str,
    raw: Any,
) -> None:
    """Structured warning for a value that was present but unusable."""
    logger.warning(
        "malformed exercise value ignored: endpoint=%s exercise=%r "
        "session_id=%s exercise_row_id=%s field=%s raw=%r normalized=None",
        endpoint, exercise_name, session_id, row_id, field_name, raw,
    )


@dataclass
class NormalizedSet:
    """One set from ``actual_sets`` with every field arithmetic-safe."""

    reps: int | None = None
    weight_kg: float | None = None
    rpe: float | None = None


@dataclass
class NormalizedExercisePerformance:
    """One exercise occurrence with all numbers validated.

    ``volume`` and the best-set fields are computed only from valid values;
    anything unreadable is None rather than a crash or a made-up number.
    """

    sets: int | None = None
    reps: list[int] = field(default_factory=list)
    average_reps: float | None = None
    best_reps: int | None = None
    weight_kg: float | None = None
    best_set_weight_kg: float | None = None
    rpe: float | None = None
    rir: float | None = None
    volume: float | None = None
    set_detail: list[NormalizedSet] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)


def normalize_performance(
    record: dict[str, Any],
    *,
    endpoint: str = "exercise_metrics",
    exercise_name: str | None = None,
    session_id: Any = None,
    row_id: Any = None,
) -> NormalizedExercisePerformance:
    """Normalize one exercise record (aggregate columns + optional
    ``actual_sets``) into a :class:`NormalizedExercisePerformance`.

    Per-set data takes precedence over the aggregate columns wherever both
    exist. Volume is summed per set when per-set reps+weight are usable;
    otherwise it falls back to ``sets × reps × weight`` (or, for a legacy
    rep *list*, one entry per set × the aggregate weight). Missing or
    malformed pieces make the derived value None — never an exception.
    """

    dropped_fields: list[str] = []

    def dropped(field_name: str, raw: Any) -> None:
        dropped_fields.append(field_name)
        log_malformed_value(endpoint, exercise_name, session_id, row_id, field_name, raw)

    def scalar(field_name: str, parser) -> Any:
        raw = record.get(field_name)
        parsed = parser(raw)
        if raw is not None and parsed is None:
            dropped(field_name, raw)
        return parsed

    sets_agg = scalar("sets", parse_int)
    weight_agg = scalar("weight_kg", parse_float)
    rpe = scalar("rpe", parse_float)
    rir = scalar("rir", parse_float)

    raw_reps = record.get("reps")
    agg_reps = parse_reps(raw_reps)
    if raw_reps is not None and not agg_reps:
        dropped("reps", raw_reps)

    raw_sets = record.get("actual_sets")
    if raw_sets is not None and not isinstance(raw_sets, list):
        dropped("actual_sets", raw_sets)
        raw_sets = None
    set_detail: list[NormalizedSet] = []
    for index, entry in enumerate(raw_sets or []):
        if not isinstance(entry, dict):
            dropped(f"actual_sets[{index}]", entry)
            continue
        normalized = NormalizedSet()
        for field_name, parser in (
            ("reps", parse_int), ("weight_kg", parse_float), ("rpe", parse_float),
        ):
            raw = entry.get(field_name)
            parsed = parser(raw)
            if raw is not None and parsed is None:
                dropped(f"actual_sets[{index}].{field_name}", raw)
            setattr(normalized, field_name, parsed)
        set_detail.append(normalized)

    per_set_reps = [s.reps for s in set_detail if s.reps is not None]
    per_set_weights = [s.weight_kg for s in set_detail if s.weight_kg is not None]

    reps_list = per_set_reps or agg_reps
    sets = len(set_detail) if set_detail else sets_agg
    weight = max(per_set_weights) if per_set_weights else weight_agg
    if rpe is None:
        per_set_rpes = [s.rpe for s in set_detail if s.rpe is not None]
        if per_set_rpes:
            rpe = round(sum(per_set_rpes) / len(per_set_rpes), 1)

    volume: float | None = None
    pairs = [
        (s.reps, s.weight_kg)
        for s in set_detail
        if s.reps is not None and s.weight_kg is not None
    ]
    if pairs:
        volume = sum(reps * weight_kg for reps, weight_kg in pairs)
    elif not set_detail and weight_agg is not None:
        if len(agg_reps) > 1:  # legacy list: one entry per set
            volume = sum(agg_reps) * weight_agg
        elif len(agg_reps) == 1 and sets_agg is not None:
            volume = sets_agg * agg_reps[0] * weight_agg

    return NormalizedExercisePerformance(
        sets=sets,
        reps=reps_list,
        average_reps=round(sum(reps_list) / len(reps_list), 2) if reps_list else None,
        best_reps=max(reps_list) if reps_list else None,
        weight_kg=weight,
        best_set_weight_kg=weight,
        rpe=rpe,
        rir=rir,
        volume=round(volume, 1) if volume is not None else None,
        set_detail=set_detail,
        dropped_fields=dropped_fields,
    )
