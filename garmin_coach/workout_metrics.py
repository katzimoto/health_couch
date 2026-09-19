"""Structured, multi-source workout metrics — power, cadence, METs and friends.

One workout is often measured by two devices at once, each good at something
different: the watch has the wearer's heart rate and Garmin's training load, the
gym machine has distance, power, cadence, METs and its own calorie figure, and
the two never talk to each other. Storing the machine's numbers in a free-text
note makes them invisible to every tool; picking one device and discarding the
other throws away real measurements.

So observations are stored *per metric, per source*, and one of them is
**selected** as the canonical value:

* every observation keeps its ``source``, its originating activity, its unit,
  its aggregation (average/maximum/total) and optional confidence/metadata;
* selection is a documented per-metric rule — heart rate from the watch, power
  and cadence and distance from the machine, training load from Garmin — with
  the losing observations kept and inspectable, never overwritten;
* a manual override always wins, because the user knows which device was
  actually strapped on;
* re-importing the same observation updates it in place, so a repeated sync can
  never double-count a metric any more than it double-counts a workout.

This module is pure: normalization, the rule table and the selection algorithm
take plain dicts, so the interesting decisions are unit-testable without a
database. :class:`~garmin_coach.database.Database` wires them to rows, and the
canonical *summary* columns (duration/distance/calories/HR/load) keep going
through :mod:`~garmin_coach.workout_merge` — one merge system, as issue #9
established, not two.
"""

from __future__ import annotations

from typing import Any

# ── Metric vocabulary ─────────────────────────────────────────────────────────
# Known metrics get a canonical name, a canonical unit and the aggregations that
# make sense for them. Unknown metric names are *accepted* (the representation
# is deliberately extensible) — they simply carry the caller's unit and fall
# back to the default selection rule.
KNOWN_METRICS: dict[str, dict[str, Any]] = {
    "power": {"unit": "W", "aggregations": ("avg", "max"), "domain": "equipment"},
    "cadence": {"unit": "rpm", "aggregations": ("avg", "max"), "domain": "equipment"},
    "mets": {"unit": "METs", "aggregations": ("avg",), "domain": "equipment"},
    "speed": {"unit": "m/s", "aggregations": ("avg", "max"), "domain": "equipment"},
    "resistance": {"unit": "level", "aggregations": ("avg", "max"), "domain": "equipment"},
    "incline": {"unit": "%", "aggregations": ("avg", "max"), "domain": "equipment"},
    "stroke_rate": {"unit": "spm", "aggregations": ("avg", "max"), "domain": "equipment"},
    "heart_rate": {"unit": "bpm", "aggregations": ("avg", "max"), "domain": "physiology"},
    "respiration_rate": {"unit": "brpm", "aggregations": ("avg", "max"), "domain": "physiology"},
}

# Spellings that mean one of the above. Anything not listed passes through
# normalized (lowercased, underscored) rather than being rejected.
_METRIC_ALIASES = {
    "watts": "power", "w": "power", "avg_watts": "power", "output": "power",
    "rpm": "cadence", "revolutions_per_minute": "cadence", "pedal_cadence": "cadence",
    "met": "mets", "met_s": "mets", "metabolic_equivalent": "mets",
    "hr": "heart_rate", "bpm": "heart_rate", "heartrate": "heart_rate",
    "spm": "stroke_rate", "strokes_per_minute": "stroke_rate",
    "velocity": "speed",
}

# Aggregation spellings → canonical form.
_AGGREGATION_ALIASES = {
    "avg": "avg", "average": "avg", "mean": "avg", "": "avg", "none": "avg",
    "max": "max", "maximum": "max", "peak": "max",
    "min": "min", "minimum": "min",
    "total": "total", "sum": "total", "cumulative": "total",
}

# A prefix on the metric name that really names the aggregation
# (``avg_power`` → power/avg, ``max_cadence`` → cadence/max).
_AGGREGATION_PREFIXES = (
    ("avg_", "avg"), ("average_", "avg"), ("mean_", "avg"),
    ("max_", "max"), ("maximum_", "max"), ("peak_", "max"),
    ("min_", "min"), ("minimum_", "min"),
    ("total_", "total"),
)

# ── Sources ───────────────────────────────────────────────────────────────────
# Arbitrary source strings are allowed (a specific machine's name is useful
# provenance), but selection reasons about *buckets*: what kind of device it is.
SOURCE_BUCKETS = ("garmin", "apple", "equipment", "photo", "manual")

_SOURCE_ALIASES = {
    "garmin": "garmin", "garmin_merged": "garmin", "merged": "garmin", "watch": "garmin",
    "apple": "apple", "apple_health": "apple", "healthkit": "apple",
    "manual": "manual", "user": "manual", "telegram": "manual", "import": "manual",
    "photo": "photo", "screenshot": "photo", "image": "photo", "ocr": "photo",
}

# Substrings that identify a piece of gym equipment / a machine console.
_EQUIPMENT_HINTS = (
    "star_trac", "startrac", "equipment", "machine", "console", "treadmill_console",
    "technogym", "life_fitness", "lifefitness", "concept2", "concept_2", "wattbike",
    "keiser", "peloton", "matrix", "precor", "spin_bike", "ergometer", "erg",
)

# ── Selection rules ───────────────────────────────────────────────────────────
# Per metric, which *kind* of device to believe, best first. The reasoning is
# about what each device actually measures rather than about brand:
#   * the watch is on the wearer, so it owns heart rate and Garmin's own load;
#   * the machine measures its own flywheel, so it owns power, cadence,
#     resistance, distance and the calorie/MET figures derived from them.
# Tune this table, not the call sites. A manual override beats all of it.
DEFAULT_SOURCE_RULES: dict[str, tuple[str, ...]] = {
    "heart_rate": ("garmin", "apple", "equipment", "manual", "photo"),
    "respiration_rate": ("garmin", "apple", "manual", "photo", "equipment"),
    "power": ("equipment", "garmin", "apple", "photo", "manual"),
    "cadence": ("equipment", "garmin", "apple", "photo", "manual"),
    "mets": ("equipment", "garmin", "apple", "photo", "manual"),
    "speed": ("equipment", "garmin", "apple", "photo", "manual"),
    "resistance": ("equipment", "photo", "manual", "garmin", "apple"),
    "incline": ("equipment", "photo", "manual", "garmin", "apple"),
    "stroke_rate": ("equipment", "garmin", "apple", "photo", "manual"),
    "training_load": ("garmin", "manual", "apple", "equipment", "photo"),
}

# Metrics nobody has a rule for: prefer the device that physically measured the
# thing (the machine), then the watch, then anything a human typed.
DEFAULT_PRIORITY: tuple[str, ...] = ("equipment", "garmin", "apple", "photo", "manual")

# Two observations of one metric that differ by more than this fraction are
# reported as a conflict — both are kept either way, this only controls whether
# the response calls attention to the disagreement.
CONFLICT_TOLERANCE = 0.02


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def normalize_source(source: Any) -> str:
    """The source string as stored — lowercased and underscored, never dropped."""
    text = str(source or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text or "manual"


def source_bucket(source: Any) -> str:
    """Which kind of device a source string represents.

    Known names map directly; anything containing an equipment hint (a machine
    brand, "console", "erg") is equipment; everything else is treated as a
    manual entry, which is the least authoritative bucket and therefore the
    safe default for an unrecognised name.
    """
    text = normalize_source(source)
    if text in _SOURCE_ALIASES:
        return _SOURCE_ALIASES[text]
    if any(hint in text for hint in _EQUIPMENT_HINTS):
        return "equipment"
    return "manual"


def normalize_metric(name: Any, aggregation: Any = None) -> tuple[str, str]:
    """``(metric, aggregation)`` from a metric name that may carry its own prefix.

    ``("avg_power", None)`` → ``("power", "avg")``; ``("power", "max")`` →
    ``("power", "max")``; an explicit ``aggregation`` wins over a prefix.
    Unknown metric names survive normalization instead of being rejected —
    the representation is meant to be extensible.
    """
    text = str(name or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in text:
        text = text.replace("__", "_")

    prefix_aggregation: str | None = None
    for prefix, agg in _AGGREGATION_PREFIXES:
        if text.startswith(prefix) and len(text) > len(prefix):
            prefix_aggregation = agg
            text = text[len(prefix):]
            break

    text = _METRIC_ALIASES.get(text, text)
    explicit = _AGGREGATION_ALIASES.get(
        str(aggregation or "").strip().lower(), None
    ) if aggregation is not None else None
    resolved = explicit or prefix_aggregation or "avg"
    return text, resolved


def canonical_unit(metric: str, unit: Any = None) -> str | None:
    """The unit to store: the caller's when given, else the known default.

    Units are recorded, never silently converted — a value whose unit we don't
    recognise is still worth keeping with the unit the source stated.
    """
    if unit not in (None, ""):
        return str(unit).strip()
    known = KNOWN_METRICS.get(metric)
    return known["unit"] if known else None


def metric_key(metric: str, aggregation: str) -> str:
    """The identity a selection is made over (``power:avg``)."""
    return f"{metric}:{aggregation}"


def normalize_observation(
    raw: dict[str, Any],
    *,
    default_source: Any = None,
    default_source_activity_id: int | None = None,
) -> dict[str, Any] | None:
    """One incoming measurement in storage shape, or None if unusable.

    An observation needs a metric name and a numeric value; everything else has
    a sensible default. Unusable input returns None rather than raising, so one
    bad entry in a batch never costs the rest.
    """
    if not isinstance(raw, dict):
        return None
    value = _num(raw.get("value"))
    if value is None:
        return None
    metric, aggregation = normalize_metric(
        raw.get("metric") or raw.get("name"), raw.get("aggregation")
    )
    if not metric:
        return None
    source = normalize_source(raw.get("source") or default_source)
    return {
        "metric": metric,
        "aggregation": aggregation,
        "value": value,
        "unit": canonical_unit(metric, raw.get("unit")),
        "source": source,
        "source_bucket": source_bucket(source),
        "source_activity_id": raw.get("source_activity_id", default_source_activity_id),
        "source_ref": raw.get("source_ref"),
        "confidence": _num(raw.get("confidence")),
        "is_override": bool(raw.get("is_override")),
    }


def normalize_observations(
    metrics: Any,
    *,
    default_source: Any = None,
    default_source_activity_id: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Normalize a batch, returning ``(observations, rejected)``.

    ``metrics`` may be a list of dicts or a flat mapping
    (``{"avg_power": 82, "avg_cadence": 54}``) — the mapping form is what a
    conversational tool call naturally produces. Rejected entries are returned
    with a reason instead of being silently dropped.
    """
    entries: list[dict[str, Any]] = []
    if isinstance(metrics, dict):
        for name, value in metrics.items():
            entries.append(
                {**value, "metric": value.get("metric", name)}
                if isinstance(value, dict) else {"metric": name, "value": value}
            )
    elif isinstance(metrics, list):
        entries = [m for m in metrics]
    elif metrics is not None:
        return [], [{"raw": metrics, "reason": "metrics must be a list or a mapping"}]

    out: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for entry in entries:
        observation = normalize_observation(
            entry,
            default_source=default_source,
            default_source_activity_id=default_source_activity_id,
        )
        if observation is None:
            rejected.append({
                "raw": entry,
                "reason": "needs a metric name and a numeric value",
            })
        else:
            out.append(observation)
    return out, rejected


def rule_for(metric: str, rules: dict[str, tuple[str, ...]] | None = None) -> tuple[str, ...]:
    """The source priority for a metric (its rule, or the documented default)."""
    table = rules or DEFAULT_SOURCE_RULES
    return table.get(metric, DEFAULT_PRIORITY)


def select_observation(
    observations: list[dict[str, Any]],
    metric: str,
    *,
    rules: dict[str, tuple[str, ...]] | None = None,
    override_source: str | None = None,
) -> dict[str, Any] | None:
    """Pick the canonical observation for one metric from its candidates.

    Order of authority: an explicit ``override_source`` for this metric, then
    any observation flagged ``is_override`` (a stored manual choice), then the
    metric's source rule. Ties inside one bucket break to the higher stated
    confidence and then to the lowest source name, so the choice is
    deterministic rather than insertion-ordered.
    """
    if not observations:
        return None
    if override_source:
        wanted = normalize_source(override_source)
        explicit = [o for o in observations if o["source"] == wanted]
        if explicit:
            return _best(explicit)
    flagged = [o for o in observations if o.get("is_override")]
    if flagged:
        return _best(flagged)
    priority = rule_for(metric, rules)
    for bucket in priority:
        matching = [o for o in observations if o["source_bucket"] == bucket]
        if matching:
            return _best(matching)
    return _best(observations)


def _best(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        candidates,
        key=lambda o: (-(o.get("confidence") or 0.0), str(o.get("source") or "")),
    )[0]


def select_metrics(
    observations: list[dict[str, Any]],
    *,
    rules: dict[str, tuple[str, ...]] | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve every metric present, keeping the alternatives.

    Returns ``{metric_key: {selected, alternatives, rule, reason, conflict}}``.
    The losing observations are *in the payload*, not discarded — when two
    devices disagree the disagreement is part of the answer.
    """
    overrides = {
        metric_key(*normalize_metric(k)): v for k, v in (overrides or {}).items()
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        key = metric_key(observation["metric"], observation["aggregation"])
        grouped.setdefault(key, []).append(observation)

    resolved: dict[str, Any] = {}
    for key, candidates in sorted(grouped.items()):
        metric = candidates[0]["metric"]
        override_source = overrides.get(key)
        selected = select_observation(
            candidates, metric, rules=rules, override_source=override_source
        )
        alternatives = [o for o in candidates if o is not selected]
        values = [o["value"] for o in candidates]
        spread = (max(values) - min(values)) if len(values) > 1 else 0.0
        largest = max(abs(v) for v in values) if values else 0.0
        conflict = bool(alternatives) and largest > 0 and (spread / largest) > CONFLICT_TOLERANCE
        if override_source:
            reason = f"manual override: {override_source} selected for {metric}"
        elif selected.get("is_override"):
            reason = f"stored manual override on the {selected['source']} observation"
        elif len(candidates) == 1:
            reason = f"only {selected['source']} reported {key}"
        else:
            reason = (
                f"{selected['source_bucket']} wins {metric} by rule "
                f"{' > '.join(rule_for(metric, rules))}"
            )
        resolved[key] = {
            "metric": metric,
            "aggregation": candidates[0]["aggregation"],
            "value": selected["value"],
            "unit": selected["unit"],
            "source": selected["source"],
            "source_bucket": selected["source_bucket"],
            "source_activity_id": selected.get("source_activity_id"),
            "confidence": selected.get("confidence"),
            "selection_reason": reason,
            "alternatives": [
                {
                    "value": o["value"], "unit": o["unit"], "source": o["source"],
                    "source_bucket": o["source_bucket"],
                    "source_activity_id": o.get("source_activity_id"),
                    "confidence": o.get("confidence"),
                }
                for o in alternatives
            ],
            "conflict": conflict,
            **({
                "conflict_note": (
                    "sources disagree by more than "
                    f"{CONFLICT_TOLERANCE * 100:.0f}% — both values are kept; "
                    "set_workout_metric_source overrides the choice"
                )
            } if conflict else {}),
        }
    return resolved


def observation_identity(observation: dict[str, Any]) -> tuple[Any, ...]:
    """What makes two stored observations *the same measurement*.

    Re-importing the same source's reading of the same metric updates that row
    rather than adding another, which is what keeps repeated syncs from
    inflating a metric the way they must not inflate a workout.
    """
    return (
        observation["metric"],
        observation["aggregation"],
        observation["source"],
        observation.get("source_activity_id"),
    )
