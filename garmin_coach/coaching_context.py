"""Unified daily coaching context — the one call a daily plan is built from.

:func:`build_coaching_context` assembles everything a coach (human, LLM, or the
07:30 scheduler) needs to answer *"based on all available data, what should I do
today?"* in a single structured payload: data freshness, profile, recovery,
sleep, activity, training load, recent workouts, strength history, body
composition, nutrition, hydration, a pending training plan, flags, data-quality
warnings, and — optionally — a concrete recommendation.

The two decision functions are deliberately **pure** (no DB, no clock, no
network) so the coaching logic is unit-testable in isolation:

* :func:`classify_recovery` → ``{status, confidence, reasons}`` from the
  recovery signals, weighing sleep against HRV / resting-HR / stress / load
  rather than gating on sleep alone.
* :func:`build_recommendation` → a structured training decision plus step /
  cardio / sleep / hydration / nutrition targets and one top priority.

Only summaries flow through here — the same security boundary the rest of the
system keeps.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from .analysis import Analyzer
from .database import Database

# Recovery status ladder, best → worst. Each maps to a training posture.
RECOVERY_STATUSES = ("good", "moderate", "low", "compromised")

# How far resting HR must sit above its 28-day baseline before it counts as an
# elevation signal (bpm). Matches the analyzer's own flag threshold.
_RHR_ELEVATED_DELTA = 5.0


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_recovery(
    *,
    sleep_hours: float | None,
    sleep_score: float | None,
    hrv: float | None,
    hrv_status: str | None,
    resting_hr: float | None,
    resting_hr_delta: float | None,
    avg_stress: float | None,
    body_battery: float | None,
    acr_ratio: float | None,
    readiness: dict[str, Any] | None,
    sleep_target_hours: float,
    sleep_minimum_recovery_hours: float,
) -> dict[str, Any]:
    """Classify recovery from the day's signals — transparent, not a black box.

    Returns ``{"status", "confidence", "reasons", "signals"}`` where ``status``
    is one of :data:`RECOVERY_STATUSES`. The design intent (see the mission's
    decision logic): a short night alone reduces, it does not cancel; HRV
    meaningfully below balance, a resting-HR jump, high stress or a load spike
    push toward active recovery; reported pain/illness escalates to rest.
    ``confidence`` scales with how many signals were actually available.
    """
    reasons: list[str] = []
    # severity accumulates; higher = worse recovery.
    severity = 0.0
    available = 0
    total_signals = 6  # sleep, hrv, resting_hr, stress, body_battery, load

    readiness = readiness or {}

    # ── Sleep (duration vs configured target; never a hard-coded 8h) ──────────
    sh = _num(sleep_hours)
    if sh is not None:
        available += 1
        if sh < sleep_minimum_recovery_hours:
            # A very short night reduces, it does not cancel: sleep alone stays
            # in "moderate" territory (severity < the "low" threshold). It's the
            # combination with HRV / resting-HR / stress that escalates.
            severity += 1.5
            reasons.append(
                f"Sleep was {sh:.1f}h, below the {sleep_minimum_recovery_hours:.1f}h "
                "minimum-recovery floor"
            )
        elif sh < sleep_target_hours:
            severity += 1.0
            reasons.append(
                f"Sleep was {sh:.1f}h, below the configured {sleep_target_hours:.1f}h target"
            )
        else:
            reasons.append(f"Sleep was {sh:.1f}h, at or above the {sleep_target_hours:.1f}h target")

    # ── HRV status/trend ──────────────────────────────────────────────────────
    if hrv_status:
        available += 1
        status_l = str(hrv_status).lower()
        if status_l in ("unbalanced", "low", "poor"):
            severity += 1.5
            reasons.append(f"HRV status is {hrv_status}")
        elif status_l == "balanced":
            reasons.append("HRV is balanced")
        else:
            reasons.append(f"HRV status is {hrv_status}")
    elif _num(hrv) is not None:
        available += 1
        reasons.append(f"HRV is {_num(hrv):.0f}ms")

    # ── Resting HR vs baseline ────────────────────────────────────────────────
    rhr_delta = _num(resting_hr_delta)
    if rhr_delta is not None:
        available += 1
        if rhr_delta >= _RHR_ELEVATED_DELTA:
            severity += 1.5
            reasons.append(f"Resting heart rate is +{rhr_delta:.0f} bpm vs baseline")
        else:
            reasons.append("Resting heart rate is within baseline")
    elif _num(resting_hr) is not None:
        available += 1
        reasons.append(f"Resting heart rate is {_num(resting_hr):.0f} bpm")

    # ── Stress ────────────────────────────────────────────────────────────────
    stress = _num(avg_stress)
    if stress is not None:
        available += 1
        if stress >= 60:
            severity += 1.0
            reasons.append(f"Average stress is high ({stress:.0f})")
        else:
            reasons.append(f"Average stress is {stress:.0f}")

    # ── Body Battery (when the device reports it) ─────────────────────────────
    bb = _num(body_battery)
    if bb is not None:
        available += 1
        if bb < 30:
            severity += 1.0
            reasons.append(f"Body Battery is low ({bb:.0f})")
        else:
            reasons.append(f"Body Battery is {bb:.0f}")

    # ── Recent training load (acute:chronic) ──────────────────────────────────
    acr = _num(acr_ratio)
    if acr is not None:
        available += 1
        total_signals += 0  # already counted in denominator via +1 below
        if acr > 1.5:
            severity += 1.5
            reasons.append(f"Acute-to-chronic training load is elevated ({acr:.2f})")
        elif acr < 0.8:
            reasons.append(f"Acute-to-chronic training load is low ({acr:.2f})")
        else:
            reasons.append(f"Acute-to-chronic training load is balanced ({acr:.2f})")

    # ── Subjective readiness / pain / illness (escalation) ───────────────────
    escalate = False
    soreness = _num(readiness.get("soreness_1_10"))
    energy = _num(readiness.get("energy_1_10"))
    pain_areas = readiness.get("pain_areas")
    if soreness is not None and soreness >= 7:
        severity += 1.5
        reasons.append("Reported soreness is high")
    if energy is not None and energy <= 3:
        severity += 1.0
        reasons.append("Reported energy is low")
    if pain_areas:
        escalate = True
        reasons.append(f"Reported pain: {pain_areas}")
    if (readiness.get("notes") or "").lower().count("ill") or (
        readiness.get("mood") or ""
    ).lower() in ("sick", "ill"):
        escalate = True
        reasons.append("Possible illness reported")

    # ── Map severity → status ────────────────────────────────────────────────
    if escalate:
        status = "compromised"
    elif severity >= 3.5:
        status = "compromised"
    elif severity >= 2.0:
        status = "low"
    elif severity >= 1.0:
        status = "moderate"
    else:
        status = "good"

    # Confidence reflects breadth of evidence: more signals present → surer.
    confidence = round(min(0.5 + 0.5 * (available / total_signals), 0.98), 2)
    if available == 0:
        confidence = 0.2

    return {
        "status": status,
        "confidence": confidence,
        "reasons": reasons,
        "signals_available": available,
        "signals_expected": total_signals,
    }


# Training decision per recovery status, and the per-status intensity.
_DECISION_BY_STATUS = {
    "good": ("normal_strength", "moderate_to_hard"),
    "moderate": ("reduced_strength", "easy_to_moderate"),
    "low": ("active_recovery", "easy"),
    "compromised": ("rest", "rest"),
}


def build_recommendation(
    *,
    recovery: dict[str, Any],
    profile: dict[str, Any],
    sleep_hours: float | None,
    sleep_target_hours: float,
    hydration_targets: dict[str, Any],
    is_training_day: bool,
    pending_plan: dict[str, Any] | None,
    steps_today: float | None = None,
) -> dict[str, Any]:
    """A structured recommendation (not just prose) from the recovery call and
    the user's schedule/targets. Pure — safe to unit-test."""
    status = recovery.get("status", "moderate")
    decision, intensity = _DECISION_BY_STATUS.get(status, ("reduced_strength", "easy_to_moderate"))

    # A rest/recovery day still gets a walking target; a normal day pushes it up.
    if status in ("good", "moderate"):
        step_target = 9000
    else:
        step_target = 7000

    # Sleep target for tonight: nudge toward the top of the preferred band when
    # sleep has been short, else hold the baseline target.
    sleep_target_tonight = sleep_target_hours
    pref_max = _num(profile.get("sleep_preferred_max_hours"))
    if sleep_hours is not None and sleep_hours < sleep_target_hours and pref_max:
        sleep_target_tonight = pref_max

    # Hydration: training days (and the recommendation to train) use the higher
    # target; otherwise the baseline. Missing intake is never treated as zero.
    if is_training_day and decision in ("normal_strength", "reduced_strength"):
        hydration_target_ml = hydration_targets.get("training_day_ml")
    else:
        hydration_target_ml = hydration_targets.get("baseline_ml")

    # Cardio target: Zone-2 on easy/recovery days, optional otherwise.
    if decision == "active_recovery":
        cardio_target = "20–30 minutes easy Zone 2 (walk or light bike) — keep it conversational"
    elif decision == "rest":
        cardio_target = "Optional gentle walk only; no structured cardio today"
    else:
        cardio_target = "Optional 30 minutes easy Zone 2 if you have time"

    # Suggested session skeleton (details come from the plan/history layer).
    suggested_session: dict[str, Any] = {}
    if decision == "normal_strength":
        suggested_session = {
            "type": "full_body_strength",
            "guidance": "Train normally: your planned working weights, "
            "3 working sets, 2–3 reps in reserve, avoid failure.",
        }
    elif decision == "reduced_strength":
        suggested_session = {
            "type": "full_body_strength_reduced",
            "guidance": "Keep normal working weights but cut 3 sets to 2, "
            "keep 2–3 reps in reserve, and skip anything that feels off.",
        }
    elif decision == "active_recovery":
        suggested_session = {
            "type": "active_recovery",
            "guidance": "Swap the lifting session for easy movement, mobility, "
            "and a Zone-2 walk. Re-assess tomorrow.",
        }
    else:  # rest
        suggested_session = {
            "type": "rest",
            "guidance": "Full rest today. If symptoms are concerning (chest pain, "
            "fainting, severe breathlessness) seek medical advice — this is not a diagnosis.",
        }
    if pending_plan and decision in ("normal_strength", "reduced_strength"):
        suggested_session["planned_title"] = pending_plan.get("title")
        suggested_session["planned_plan_id"] = pending_plan.get("id")

    nutrition_priorities: list[str] = []
    protein_target = profile.get("protein_target_g")
    if protein_target:
        nutrition_priorities.append(f"Hit {protein_target:g} g protein to support recomposition")
    if profile.get("calorie_target"):
        nutrition_priorities.append(
            f"Stay near your {profile['calorie_target']:g} kcal target"
        )
    if profile.get("fiber_target_g"):
        nutrition_priorities.append(f"Reach {profile['fiber_target_g']:g} g fiber")

    # Top priority: the single most important thing today.
    if status == "compromised":
        top_priority = "Rest and recover — training can wait until the signals improve"
    elif status == "low":
        top_priority = "Prioritise recovery with active movement and an early night"
    elif sleep_hours is not None and sleep_hours < sleep_target_hours:
        top_priority = "Recover from short sleep while maintaining training consistency"
    elif is_training_day and decision.startswith(("normal", "reduced")):
        top_priority = "Get your planned strength session in and log every set"
    else:
        top_priority = "Stay consistent: movement, protein, and hydration"

    return {
        "training_decision": decision,
        "training_intensity": intensity,
        "suggested_session": suggested_session,
        "step_target": step_target,
        "cardio_target": cardio_target,
        "sleep_target_hours": round(sleep_target_tonight, 1),
        "hydration_target_ml": hydration_target_ml,
        "nutrition_priorities": nutrition_priorities,
        "top_priority": top_priority,
    }


def detect_workout_quality_warnings(workouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag physiologically suspicious workout rows (never delete them).

    Catches the cases the mission calls out: zero distance on a running/walking
    activity, implausible average speed, and distance grossly inconsistent with
    duration. Each warning names the activity and field so a human can correct
    it; sensitive calculations can choose to exclude flagged rows.
    """
    warnings: list[dict[str, Any]] = []
    for w in workouts:
        aid = w.get("activity_id")
        wtype = (w.get("type") or "").lower()
        dist = _num(w.get("distance_m"))
        dur = _num(w.get("duration_s"))
        is_distance_sport = any(
            k in wtype for k in ("run", "walk", "cycl", "bike", "row", "swim")
        )
        if is_distance_sport and dur and dur > 300 and (dist is None or dist == 0):
            warnings.append({
                "activity_id": aid, "field": "distance_m", "status": "suspicious",
                "reason": f"{wtype or 'distance'} activity of {dur/60:.0f} min has zero/no distance",
                "action": "excluded_from_pace_calcs",
            })
        if dist and dur and dur > 0:
            speed_ms = dist / dur
            # >12.5 m/s (~45 km/h) is faster than a human runs/rides casually.
            if speed_ms > 12.5 and "cycl" not in wtype and "bike" not in wtype:
                warnings.append({
                    "activity_id": aid, "field": "distance_m/duration_s",
                    "status": "suspicious",
                    "reason": f"implausible average speed {speed_ms*3.6:.0f} km/h",
                    "action": "flag_for_review",
                })
    return warnings


def _staleness(last_pull: dict[str, Any] | None) -> dict[str, Any]:
    if not last_pull or not last_pull.get("ts"):
        return {"synced_ever": False, "minutes_since_sync": None, "stale": True}
    ts = last_pull["ts"]
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            ts = None
    minutes = None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        minutes = round((datetime.now(timezone.utc) - ts).total_seconds() / 60, 1)
    return {
        "synced_ever": True,
        "last_synced_day": last_pull.get("day"),
        "minutes_since_sync": minutes,
        "stale": minutes is None or minutes > 90,
    }


def build_coaching_context(
    db: Database,
    day: str | None = None,
    refresh_if_stale: bool = False,
    include_recommendation: bool = True,
    garmin_sync: Callable[[], Any] | None = None,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """Assemble the full daily coaching context for ``day`` (default today).

    ``refresh_if_stale`` + ``garmin_sync``: when the last Garmin pull is stale
    (>90 min) and a sync callback is supplied, it is invoked before reading, and
    the outcome recorded under ``data_freshness.refresh``. The callback is
    injected (not imported) so this stays testable offline and the scheduler can
    reuse the exact same read path an interactive conversation uses.

    ``data_freshness.sources`` reports, per source, whether data was available —
    so a caller can see what was retrieved and what was missing instead of a
    blanket "connector unavailable".
    """
    from .config import settings

    target_day = day or date.today().isoformat()
    tz = timezone_name or settings.timezone

    last_pull = db.last_pull()
    freshness = _staleness(last_pull)
    freshness["refresh"] = None
    if refresh_if_stale and freshness.get("stale") and garmin_sync is not None:
        try:
            freshness["refresh"] = {"attempted": True, "result": garmin_sync()}
            last_pull = db.last_pull()
            new_fresh = _staleness(last_pull)
            freshness.update({k: new_fresh[k] for k in ("minutes_since_sync", "stale", "last_synced_day") if k in new_fresh})
        except Exception as exc:  # noqa: BLE001 — surface, never crash the read
            freshness["refresh"] = {"attempted": True, "error": str(exc)}

    analyzer = Analyzer(db)
    report = analyzer.report()
    rows = db.daily_summary(days=28)
    # Row for the requested day, else the latest available (with a note).
    day_row = next((r for r in rows if r.get("day") == target_day), None)
    used_row = day_row or (rows[-1] if rows else {})

    profile = db.get_profile() or {}
    sleep_target = db.sleep_target_for(target_day)
    sleep_min_recovery = db.sleep_minimum_recovery_hours()
    hydration_targets = db.hydration_targets()

    acr = report.get("training_load", {}) if isinstance(report, dict) else {}
    acr_ratio = acr.get("ratio")
    rhr_trend = (report.get("trends", {}) or {}).get("resting_hr", {}) if isinstance(report, dict) else {}
    readiness = db.latest_readiness()

    recovery = classify_recovery(
        sleep_hours=used_row.get("sleep_hours"),
        sleep_score=used_row.get("sleep_score"),
        hrv=used_row.get("hrv"),
        hrv_status=used_row.get("hrv_status"),
        resting_hr=used_row.get("resting_hr"),
        resting_hr_delta=rhr_trend.get("delta"),
        avg_stress=used_row.get("avg_stress"),
        body_battery=used_row.get("body_battery_high"),
        acr_ratio=acr_ratio,
        readiness=readiness,
        sleep_target_hours=sleep_target,
        sleep_minimum_recovery_hours=sleep_min_recovery,
    )

    recent_workouts = db.recent_workouts(days=14)
    nutrition = db.nutrition_summary(day=target_day)
    hydration_rows = db.recent_hydration(days=2)
    hydration_today = next((h for h in hydration_rows if h.get("day") == target_day), None)
    body_rows = db.recent_body_measurements(days=60)
    strength = db.recent_strength_sessions(days=30)

    # Which day's training plan is "pending" (planned, not yet done/skipped).
    pending_plan = None
    for p in db.get_training_plans_for_day(target_day):
        if p.get("status") == "planned":
            pending_plan = p
            break

    is_training_day = _is_training_day(profile, target_day, pending_plan)

    data_quality_warnings = detect_workout_quality_warnings(recent_workouts)
    # Missing hydration is reported as unknown, never zero.
    if hydration_today is None or hydration_today.get("intake_ml") is None:
        data_quality_warnings.append({
            "field": "hydration_ml", "status": "missing",
            "reason": "No hydration logged for the day — treated as unknown, not zero",
            "action": "log_intake_or_ignore",
        })
    # Body composition older than the analysis window is stale for coaching.
    if body_rows:
        last_body_day = body_rows[-1].get("day", "")
        if last_body_day and last_body_day < db._cutoff(30):
            data_quality_warnings.append({
                "field": "body_measurement", "status": "stale",
                "reason": f"Latest body measurement is from {last_body_day} (>30d old)",
                "action": "exclude_from_recomposition_trend",
            })

    sources = {
        "garmin_summary": bool(used_row),
        "sleep": used_row.get("sleep_hours") is not None,
        "hrv": used_row.get("hrv") is not None or used_row.get("hrv_status") is not None,
        "resting_hr": used_row.get("resting_hr") is not None,
        "stress": used_row.get("avg_stress") is not None,
        "steps": used_row.get("steps") is not None,
        "training_load": acr_ratio is not None,
        "nutrition": bool(nutrition and nutrition[0].get("meal_count")),
        "hydration": hydration_today is not None and hydration_today.get("intake_ml") is not None,
        "body_composition": bool(body_rows),
        "readiness": readiness is not None,
        "strength_history": bool(strength),
    }
    freshness["sources"] = sources
    freshness["day_data_present"] = day_row is not None
    if day_row is None and used_row:
        freshness["note"] = (
            f"No data yet for {target_day}; using latest available "
            f"({used_row.get('day')})."
        )

    context: dict[str, Any] = {
        "day": target_day,
        "timezone": tz,
        "data_freshness": freshness,
        "profile": profile,
        "recovery": {
            **recovery,
            "sleep_hours": used_row.get("sleep_hours"),
            "sleep_score": used_row.get("sleep_score"),
            "hrv": used_row.get("hrv"),
            "hrv_status": used_row.get("hrv_status"),
            "resting_hr": used_row.get("resting_hr"),
            "resting_hr_vs_baseline": rhr_trend.get("delta"),
            "avg_stress": used_row.get("avg_stress"),
            "body_battery_high": used_row.get("body_battery_high"),
            "acute_chronic_ratio": acr_ratio,
            "subjective_readiness": readiness,
        },
        "sleep": {
            "hours": used_row.get("sleep_hours"),
            "score": used_row.get("sleep_score"),
            "target_hours": sleep_target,
            "minimum_recovery_hours": sleep_min_recovery,
            "debt_7d_estimate": report.get("sleep_debt_7d") if isinstance(report, dict) else None,
            "preferred_min_hours": profile.get("sleep_preferred_min_hours"),
            "preferred_max_hours": profile.get("sleep_preferred_max_hours"),
        },
        "activity": {
            "steps": used_row.get("steps"),
            "steps_trend": (report.get("trends", {}) or {}).get("steps") if isinstance(report, dict) else None,
        },
        "training_load": acr,
        "recent_workouts": recent_workouts[:10],
        "strength_history": {"recent_sessions": strength[:10]},
        "body_composition": {
            "weight_kg": used_row.get("weight_kg"),
            "body_fat": used_row.get("body_fat"),
            "recent_measurements": body_rows[-5:],
        },
        "nutrition": nutrition[0] if nutrition else None,
        "hydration": {
            "today": hydration_today,
            "targets": hydration_targets,
        },
        "pending_training_plan": pending_plan,
        "flags": report.get("flags", []) if isinstance(report, dict) else [],
        "data_quality_warnings": data_quality_warnings,
    }

    if include_recommendation:
        context["recommendation"] = build_recommendation(
            recovery=recovery,
            profile=profile,
            sleep_hours=used_row.get("sleep_hours"),
            sleep_target_hours=sleep_target,
            hydration_targets=hydration_targets,
            is_training_day=is_training_day,
            pending_plan=pending_plan,
            steps_today=used_row.get("steps"),
        )

    return context


_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _is_training_day(
    profile: dict[str, Any], target_day: str, pending_plan: dict[str, Any] | None
) -> bool:
    """A day counts as a training day if a plan is scheduled for it, or the
    weekday is in the user's preferred training days."""
    if pending_plan is not None:
        return True
    preferred = (profile.get("preferred_training_days") or "").lower()
    if not preferred:
        return False
    try:
        weekday = _WEEKDAY_NAMES[date.fromisoformat(target_day).weekday()]
    except ValueError:
        return False
    return weekday[:3] in preferred or weekday in preferred
