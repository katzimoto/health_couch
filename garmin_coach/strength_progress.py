"""Longitudinal strength progression — per exercise, over the user's own history.

``get_strength_progress(exercise, days)`` answers "is this lift going up?"
without the caller reconstructing it from individual logs or conversation
history. It reads the existing strength tables through
:func:`~garmin_coach.database.Database.exercise_history` (which already routes
every stored number through :mod:`~garmin_coach.exercise_metrics`, so a legacy
``"3"``, a rep range ``"10-12"`` or a JSON list can't crash the maths) and adds
the longitudinal half: volume computed exactly from the recorded sets, observed
PRs, progression rate, estimated 1RM with its eligibility rules, and baseline
comparison.

The rules the tests pin:

* **Per-set data wins.** Completed volume is ``Σ(reps × weight)`` over the
  recorded sets. A top-weight aggregate is *never* multiplied by all the reps —
  when only aggregates exist the figure is labelled ``aggregate_estimate`` and
  carries that caveat with it.
* **A heavier top set is not a heavier session.** ``top_set_weight_kg`` and
  ``working_weight_kg`` (the weight carried across *every* working set) are
  reported separately, and a PR says which of the two it is.
* **Aliases are conservative.** Case, spacing and punctuation fold; a short list
  of unambiguous abbreviations folds. Equipment, machine, assistance and
  variants never fold — an incline dumbbell press is not a bench press, and two
  different machines are not the same exercise.
* **Load conventions are explicit.** Per-hand dumbbell load, total barbell load,
  a machine stack, added bodyweight and assisted loads are different numbers
  with the same unit; the convention is stated rather than assumed, and
  ``unknown`` stays unknown.
* **Estimated 1RM is not a measured max.** It is produced only from a completed
  set inside the formula's valid rep range, and always names the formula.
* **Skipped and substituted work never inflates completed volume**, and a row
  whose stored values couldn't be read is excluded from PRs rather than
  producing a false one.
"""

from __future__ import annotations

import re
from typing import Any

from .exercise_metrics import parse_float, parse_int

# Unambiguous abbreviations only. Anything that could change the *movement* or
# the equipment (incline/decline, machine names, grip) is deliberately absent:
# folding those would silently compare different exercises.
_TOKEN_ALIASES = {
    "db": "dumbbell",
    "dbs": "dumbbell",
    "bb": "barbell",
    "kb": "kettlebell",
    "ohp": "overhead press",
    "rdl": "romanian deadlift",
    "bw": "bodyweight",
    "lat": "lat",
    "pulldown": "pulldown",
    "pull-down": "pulldown",
    "pull-up": "pullup",
    "pull-ups": "pullup",
    "pullups": "pullup",
    "chin-up": "chinup",
    "chinups": "chinup",
    "push-up": "pushup",
    "pushups": "pushup",
}

# Plural folding is deliberately timid: strip a trailing "s" only where doing
# so can't change the word. "press" and "lats" keep their ending; "curls" →
# "curl". Getting this wrong would merge or split real exercises.
_PLURAL_KEEP_ENDINGS = ("ss", "us", "is", "as", "os")


def _singular(token: str) -> str:
    if len(token) <= 3 or not token.endswith("s"):
        return token
    if token.endswith(_PLURAL_KEEP_ENDINGS):
        return token
    return token[:-1]


# Load-convention detection. Keyword → (convention, note). Checked in order, so
# "assisted pull-up" resolves to assisted rather than bodyweight.
_CONVENTION_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("assisted", "assist"), "assisted",
     "assistance reduces the load — a larger number means an easier set, so "
     "these are never compared with added-load numbers"),
    (("dumbbell", "kettlebell"), "per_hand",
     "weight is recorded per hand unless the log says otherwise; do not compare "
     "with a barbell total"),
    (("machine", "sled", "stack", "press machine", "cable", "pulldown", "pec deck"),
     "machine_stack",
     "a machine stack number is specific to that machine's leverage — never "
     "comparable with free weights or with a different machine"),
    (("barbell", "smith", "bench press", "squat", "deadlift", "row"), "total_load",
     "total external load including the bar, when the log follows the usual "
     "convention"),
    (("pullup", "chinup", "pushup", "dip", "bodyweight", "plank"), "bodyweight",
     "bodyweight movement — any recorded weight is *added* load, not total load"),
)

# Estimated-1RM formula and the rep range it is defensible over. Above ~10 reps
# every 1RM formula diverges badly, so the estimate is withheld rather than
# guessed.
E1RM_FORMULA = "epley"
E1RM_MIN_REPS = 1
E1RM_MAX_REPS = 10

# PRs and progression need at least this many usable sessions before a
# "progression rate" is anything but noise.
MIN_SESSIONS_FOR_RATE = 3


def normalize_exercise_name(name: str | None) -> str | None:
    """Conservative canonical form of an exercise name.

    Case, punctuation and spacing are normalised and a short list of
    unambiguous abbreviations expanded. Equipment and variant words are
    *preserved* — this never turns "incline dumbbell press" into "press".
    """
    if name is None:
        return None
    text = str(name).strip().lower()
    if not text:
        return None
    text = text.replace("_", " ").replace("/", " ")
    tokens = [t for t in re.split(r"[\s]+", re.sub(r"[^\w\s\-]", " ", text)) if t]
    out: list[str] = []
    for token in tokens:
        mapped = _TOKEN_ALIASES.get(token)
        if mapped is None:
            mapped = _TOKEN_ALIASES.get(_singular(token), None)
        out.append(mapped if mapped is not None else _singular(token))
    return " ".join(out) or None


def exercise_identity(name: str | None, machine: str | None = None) -> tuple[str | None, str | None]:
    """The identity two occurrences must share to be the same exercise.

    Equipment is part of the identity: the same movement on two different
    machines is two progressions, not one, because the stacks aren't comparable.
    """
    return normalize_exercise_name(name), normalize_exercise_name(machine)


def load_convention(name: str | None, machine: str | None = None) -> dict[str, Any]:
    """How the recorded weight for this exercise should be read.

    Returns ``{"convention", "note", "detected_from"}``. ``unknown`` is a real
    answer: an unrecognised exercise keeps its numbers but is never assumed to
    follow the barbell convention.
    """
    haystack = " ".join(
        part for part in (normalize_exercise_name(name), normalize_exercise_name(machine))
        if part
    )
    for keywords, convention, note in _CONVENTION_RULES:
        for keyword in keywords:
            if keyword in haystack:
                return {
                    "convention": convention,
                    "note": note,
                    "detected_from": keyword,
                }
    return {
        "convention": "unknown",
        "note": (
            "the log does not say whether this weight is per hand, a total load "
            "or a machine stack — it is reported as recorded and not compared "
            "with differently-conventioned entries"
        ),
        "detected_from": None,
    }


def estimate_1rm(weight_kg: float | None, reps: int | None) -> dict[str, Any]:
    """Epley estimated 1RM, or an explicit reason why not.

    ``weight × (1 + reps/30)``. Withheld outside 1–10 reps, and always labelled
    an estimate — it is not, and never becomes, a measured maximum.
    """
    weight = parse_float(weight_kg)
    rep_count = parse_int(reps)
    if weight is None or rep_count is None:
        return {
            "value_kg": None, "formula": E1RM_FORMULA, "eligible": False,
            "reason": "needs both a weight and a rep count from the same set",
        }
    if rep_count < E1RM_MIN_REPS or rep_count > E1RM_MAX_REPS:
        return {
            "value_kg": None, "formula": E1RM_FORMULA, "eligible": False,
            "reason": (
                f"{rep_count} reps is outside the {E1RM_MIN_REPS}–{E1RM_MAX_REPS} "
                "rep range the formula is defensible over"
            ),
        }
    return {
        "value_kg": round(weight * (1 + rep_count / 30.0), 1),
        "formula": E1RM_FORMULA,
        "formula_expression": "weight × (1 + reps / 30)",
        "eligible": True,
        "from_weight_kg": weight,
        "from_reps": rep_count,
        "is_estimate": True,
        "note": "an estimate from a submaximal set — not a measured 1RM",
    }


def _sets_from(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Usable per-set records (reps + weight both readable) from a history row."""
    raw = entry.get("actual_sets") or []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reps, weight = parse_int(item.get("reps")), parse_float(item.get("weight_kg"))
        if reps is None or weight is None:
            continue
        out.append({"reps": reps, "weight_kg": weight, "rpe": parse_float(item.get("rpe"))})
    return out


def session_performance(entry: dict[str, Any]) -> dict[str, Any]:
    """One exercise occurrence as a progression record.

    ``entry`` is a row from
    :meth:`~garmin_coach.database.Database.exercise_history`. Skipped or
    substituted work is marked ``counts_towards_volume: False`` and contributes
    nothing to completed volume or PRs.
    """
    status = entry.get("status")
    skipped = status in ("skipped", "substituted") or entry.get("completed") is False
    sets = _sets_from(entry)
    quality = entry.get("data_quality")

    top_set_weight = max((s["weight_kg"] for s in sets), default=None)
    working_weight = min((s["weight_kg"] for s in sets), default=None)
    if top_set_weight is None:
        top_set_weight = parse_float(entry.get("best_set_weight_kg")) or parse_float(
            entry.get("weight_kg")
        )
        working_weight = None

    if sets:
        volume = round(sum(s["reps"] * s["weight_kg"] for s in sets), 1)
        volume_basis = "per_set"
        volume_note = "exact: Σ(reps × weight) over the recorded sets"
    else:
        # No per-set data. `exercise_history` already derived an aggregate
        # estimate through the shared normalizer; carry it, labelled, rather
        # than multiplying a top weight by every rep ourselves.
        volume = parse_float(entry.get("estimated_volume_kg"))
        volume_basis = "aggregate_estimate" if volume is not None else None
        volume_note = (
            "estimated from the aggregate sets/reps/weight columns — sets at "
            "different weights are not distinguishable in this record"
            if volume is not None else "no usable sets/reps/weight recorded"
        )

    best_set = max(sets, key=lambda s: (s["weight_kg"], s["reps"]), default=None) if sets else None
    if best_set is None and top_set_weight is not None:
        best_reps = parse_int(entry.get("reps"))
        best_set = {"reps": best_reps, "weight_kg": top_set_weight, "rpe": None} if best_reps else None

    return {
        "date": entry.get("date"),
        "session_id": entry.get("session_id"),
        "session_name": entry.get("session_name"),
        "machine": entry.get("machine"),
        "status": status,
        "counts_towards_volume": not skipped,
        "sets": len(sets) or parse_int(entry.get("sets")),
        "sets_basis": "per_set" if sets else "aggregate",
        "reps": [s["reps"] for s in sets] or None,
        "avg_reps": (
            round(sum(s["reps"] for s in sets) / len(sets), 2) if sets
            else parse_float(entry.get("reps"))
        ),
        "top_set_weight_kg": top_set_weight,
        "working_weight_kg": working_weight,
        "all_sets_at_top_weight": (
            bool(sets) and working_weight is not None and working_weight == top_set_weight
        ),
        "completed_volume_kg": None if skipped else volume,
        "volume_basis": None if skipped else volume_basis,
        "volume_note": (
            "skipped or substituted — excluded from completed volume"
            if skipped else volume_note
        ),
        "best_set": best_set,
        "rpe": parse_float(entry.get("rpe")),
        "rir": parse_float(entry.get("rir")),
        "estimated_1rm": (
            estimate_1rm(best_set["weight_kg"], best_set["reps"])
            if best_set and not skipped
            else {"value_kg": None, "formula": E1RM_FORMULA, "eligible": False,
                  "reason": "no completed set with both a weight and a rep count"}
        ),
        "data_quality": quality,
        "usable_for_records": not skipped and quality is None and top_set_weight is not None,
        "substitute_exercise": entry.get("substitute_exercise"),
        "pain_note": entry.get("pain_note"),
    }


def detect_records(performances: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed weight / rep / volume records over usable sessions.

    A weight PR says whether the weight was carried across **all** working sets
    or only on a top set — the difference between "I added a heavier single" and
    "I moved the whole session up". Sessions that were skipped, substituted or
    whose stored values couldn't be read are excluded, and the exclusion is
    reported rather than silently applied.
    """
    usable = [p for p in performances if p["usable_for_records"]]
    excluded = [
        {"session_id": p["session_id"], "date": p["date"],
         "reason": "skipped or substituted" if not p["counts_towards_volume"]
         else p["data_quality"] or "no readable weight"}
        for p in performances if not p["usable_for_records"]
    ]
    out: dict[str, Any] = {
        "available": bool(usable),
        "sessions_considered": len(usable),
        "excluded_sessions": excluded,
        "basis": "the user's own logged sessions for this exercise only",
    }
    if not usable:
        out["unavailable_reason"] = (
            "no session with a readable completed weight — no record is claimed"
        )
        return out

    heaviest = max(usable, key=lambda p: (p["top_set_weight_kg"], p["date"] or ""))
    out["heaviest_weight_kg"] = {
        "value": heaviest["top_set_weight_kg"],
        "date": heaviest["date"],
        "session_id": heaviest["session_id"],
        "scope": (
            "carried across every working set" if heaviest["all_sets_at_top_weight"]
            else "top set only" if heaviest["working_weight_kg"] is not None
            else "aggregate record — per-set detail not available"
        ),
        "reps_at_weight": (heaviest.get("best_set") or {}).get("reps"),
    }

    with_volume = [p for p in usable if p["completed_volume_kg"] is not None]
    if with_volume:
        best_volume = max(with_volume, key=lambda p: p["completed_volume_kg"])
        out["highest_volume_kg"] = {
            "value": best_volume["completed_volume_kg"],
            "date": best_volume["date"],
            "session_id": best_volume["session_id"],
            "basis": best_volume["volume_basis"],
        }
    else:
        out["highest_volume_kg"] = None
        out["volume_record_unavailable_reason"] = "no session has usable volume data"

    with_reps = [p for p in usable if (p.get("best_set") or {}).get("reps")]
    if with_reps:
        most_reps = max(with_reps, key=lambda p: p["best_set"]["reps"])
        out["most_reps_in_a_set"] = {
            "value": most_reps["best_set"]["reps"],
            "weight_kg": most_reps["best_set"]["weight_kg"],
            "date": most_reps["date"],
            "session_id": most_reps["session_id"],
            "note": "more reps at a lighter weight is not a heavier lift",
        }

    eligible_e1rm = [
        p for p in usable if (p["estimated_1rm"] or {}).get("value_kg") is not None
    ]
    if eligible_e1rm:
        best = max(eligible_e1rm, key=lambda p: p["estimated_1rm"]["value_kg"])
        out["best_estimated_1rm"] = {
            **best["estimated_1rm"],
            "date": best["date"],
            "session_id": best["session_id"],
        }
    else:
        out["best_estimated_1rm"] = None
        out["e1rm_unavailable_reason"] = (
            "no completed set inside the formula's valid rep range"
        )
    return out


def progression(performances: list[dict[str, Any]]) -> dict[str, Any]:
    """Change from the earliest to the latest usable session, plus a rate.

    Absolute and percentage change against the user's *own* first recorded
    session in the window. Percentages are withheld when the baseline is zero or
    missing, and no rate is claimed below :data:`MIN_SESSIONS_FOR_RATE`.
    """
    from .activity_progress import linear_slope, percent_change

    usable = [
        p for p in performances
        if p["usable_for_records"] and p["top_set_weight_kg"] is not None and p["date"]
    ]
    usable.sort(key=lambda p: p["date"])
    out: dict[str, Any] = {
        "sessions_used": len(usable),
        "first_session": usable[0]["date"] if usable else None,
        "last_session": usable[-1]["date"] if usable else None,
    }
    if len(usable) < 2:
        out["available"] = False
        out["unavailable_reason"] = (
            "fewer than two usable sessions for this exercise in the window — "
            "no progression is claimed from a single data point"
        )
        return out
    out["available"] = True

    first, last = usable[0], usable[-1]
    out["top_set_weight_kg"] = {
        "baseline": first["top_set_weight_kg"],
        "current": last["top_set_weight_kg"],
        "absolute_change": round(
            last["top_set_weight_kg"] - first["top_set_weight_kg"], 2
        ),
        "percent_change": percent_change(
            first["top_set_weight_kg"], last["top_set_weight_kg"]
        ),
        "baseline_date": first["date"],
        "current_date": last["date"],
        "direction": "higher_is_stronger",
    }

    volumes = [p for p in usable if p["completed_volume_kg"] is not None]
    if len(volumes) >= 2:
        out["completed_volume_kg"] = {
            "baseline": volumes[0]["completed_volume_kg"],
            "current": volumes[-1]["completed_volume_kg"],
            "absolute_change": round(
                volumes[-1]["completed_volume_kg"] - volumes[0]["completed_volume_kg"], 1
            ),
            "percent_change": percent_change(
                volumes[0]["completed_volume_kg"], volumes[-1]["completed_volume_kg"]
            ),
            "mixed_basis": len({v["volume_basis"] for v in volumes}) > 1,
        }
        if out["completed_volume_kg"]["mixed_basis"]:
            out["completed_volume_kg"]["caveat"] = (
                "some sessions have exact per-set volume and others only an "
                "aggregate estimate — the change mixes two bases"
            )
    else:
        out["completed_volume_kg"] = None
        out["volume_change_unavailable_reason"] = (
            "fewer than two sessions with usable volume data"
        )

    if len(usable) >= MIN_SESSIONS_FOR_RATE:
        from datetime import date as _date

        start = _date.fromisoformat(usable[0]["date"])
        points = [
            ((_date.fromisoformat(p["date"]) - start).days / 7.0, p["top_set_weight_kg"])
            for p in usable
        ]
        slope = linear_slope(points)
        out["rate"] = {
            "top_set_weight_kg_per_week": round(slope, 3) if slope is not None else None,
            "method": "least-squares slope of top-set weight over weeks since the first session",
            "sample_sessions": len(usable),
        }
    else:
        out["rate"] = None
        out["rate_unavailable_reason"] = (
            f"fewer than {MIN_SESSIONS_FOR_RATE} usable sessions — a progression "
            "rate would be noise"
        )
    return out


def frequency(performances: list[dict[str, Any]], window_days: int) -> dict[str, Any]:
    """How often this exercise was actually trained in the window."""
    trained = [p for p in performances if p["counts_towards_volume"]]
    days = sorted({p["date"] for p in trained if p["date"]})
    weeks = max(window_days / 7.0, 1 / 7.0)
    return {
        "sessions": len(trained),
        "distinct_days": len(days),
        "sessions_per_week": round(len(trained) / weeks, 2),
        "skipped_or_substituted": len(performances) - len(trained),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
    }


# ── The service ───────────────────────────────────────────────────────────────

DEFAULT_HISTORY_LIMIT = 200


def build_exercise_progress(
    db, exercise: str, days: int = 120, *, limit: int = DEFAULT_HISTORY_LIMIT
) -> dict[str, Any]:
    """Full progression for one exercise over ``days``."""
    from .activity_progress import MAX_WINDOW_DAYS

    days = max(1, min(int(days), MAX_WINDOW_DAYS))
    history = db.exercise_history(exercise, days=days, limit=limit)
    # One canonical record per (session, occurrence): exercise_history is
    # already joined per strength-exercise row, and a merged workout links the
    # session to one canonical activity, so a session recorded by both a manual
    # log and a Garmin activity is still read once here.
    seen: set[tuple[Any, Any]] = set()
    unique: list[dict[str, Any]] = []
    for entry in history:
        key = (entry.get("session_id"), entry.get("date"), entry.get("machine"),
               entry.get("weight_kg"), entry.get("sets"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)

    performances = [session_performance(e) for e in unique]
    performances.sort(key=lambda p: p["date"] or "")

    name, machine = exercise_identity(exercise, None)
    machines = sorted({p["machine"] for p in performances if p["machine"]})
    conventions = {
        (p["machine"] or ""): load_convention(exercise, p["machine"])
        for p in performances
    } or {"": load_convention(exercise, None)}

    result: dict[str, Any] = {
        "exercise": exercise,
        "normalized_exercise": name,
        "window_days": days,
        "available": bool(performances),
        "equipment_variants": machines,
        "load_convention": load_convention(exercise, machines[0] if machines else None),
        "load_conventions_by_equipment": {
            k: v for k, v in conventions.items()
        },
        "frequency": frequency(performances, days),
        "records": detect_records(performances),
        "progression": progression(performances),
        "sessions": performances,
        "data_quality": {
            "sessions_with_unreadable_values": [
                {"session_id": p["session_id"], "date": p["date"], "note": p["data_quality"]}
                for p in performances if p["data_quality"]
            ],
            "aggregate_only_sessions": [
                p["session_id"] for p in performances
                if p["volume_basis"] == "aggregate_estimate"
            ],
            "skipped_or_substituted_sessions": [
                {"session_id": p["session_id"], "date": p["date"],
                 "status": p["status"], "substitute": p["substitute_exercise"]}
                for p in performances if not p["counts_towards_volume"]
            ],
        },
    }
    if len(machines) > 1:
        result["equipment_note"] = (
            "this exercise was logged on more than one machine/setup; loads on "
            "different machines are not equivalent and are not compared"
        )
    if not performances:
        result["unavailable_reason"] = (
            f"no logged sessions for {exercise!r} in the last {days} days"
        )
    return result


def build_strength_progress(
    db,
    exercise: str | None = None,
    days: int = 120,
    *,
    include_sessions: bool = True,
    max_exercises: int = 25,
) -> dict[str, Any]:
    """Strength progression for one exercise, or a summary across all of them.

    With ``exercise`` set this is the detailed per-exercise report. Without it,
    every exercise trained in the window gets a bounded summary (frequency,
    records, progression headline) so "how is my lifting going?" is answerable
    in one call without a second database scan per exercise.
    """
    from .activity_progress import MAX_WINDOW_DAYS

    days = max(1, min(int(days), MAX_WINDOW_DAYS))
    if exercise:
        report = build_exercise_progress(db, exercise, days=days)
        if not include_sessions:
            report.pop("sessions", None)
        return report

    names = db.recently_trained_exercises(days=days)
    truncated = len(names) > max_exercises
    summaries: list[dict[str, Any]] = []
    for name in names[:max_exercises]:
        detail = build_exercise_progress(db, name, days=days)
        prog = detail["progression"]
        summaries.append({
            "exercise": name,
            "normalized_exercise": detail["normalized_exercise"],
            "load_convention": detail["load_convention"]["convention"],
            "equipment_variants": detail["equipment_variants"],
            "sessions": detail["frequency"]["sessions"],
            "sessions_per_week": detail["frequency"]["sessions_per_week"],
            "last_day": detail["frequency"]["last_day"],
            "heaviest_weight_kg": (detail["records"] or {}).get("heaviest_weight_kg"),
            "best_estimated_1rm": (detail["records"] or {}).get("best_estimated_1rm"),
            "top_set_weight_change": prog.get("top_set_weight_kg"),
            "progression_available": prog.get("available", False),
            "progression_unavailable_reason": prog.get("unavailable_reason"),
            "data_quality_flags": len(
                detail["data_quality"]["sessions_with_unreadable_values"]
            ),
        })

    return {
        "window_days": days,
        "available": bool(summaries),
        "exercise_count": len(names),
        "exercises": summaries,
        "truncated": truncated,
        "note": (
            "per-exercise summary; call get_strength_progress(exercise=...) for "
            "the full per-session history, records and progression of one lift"
        ),
        **({} if summaries else {
            "unavailable_reason": f"no strength exercises logged in the last {days} days"
        }),
    }
