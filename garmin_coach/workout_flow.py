"""Telegram guided workout-completion flow.

``advance`` is a pure state machine over plain dicts (current step + what's
been collected so far, plus the training plan) — no database or Telegram
access — so the conversation logic is unit-testable on its own and tolerant
of partial/free-text replies. :class:`WorkoutLogFlows` wraps it with the
DB-backed conversation state (the ``workout_log_flow`` table) and the calls
that actually log the result (mirrors ``log_strength_session`` /
``mark_plan_done`` / ``mark_plan_skipped``).

Steps: awaiting_completion → (skipped: awaiting_skip_reason) or
(yes/partial: awaiting_duration → awaiting_exercises) → done.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone as tz_utc
from typing import Any

from sqlmodel import select

from .database import Database
from .models import WorkoutLogFlow
from .reminders import DEFAULT_TIMEZONE

STEP_AWAITING_COMPLETION = "awaiting_completion"
STEP_AWAITING_SKIP_REASON = "awaiting_skip_reason"
STEP_AWAITING_DURATION = "awaiting_duration"
STEP_AWAITING_EXERCISES = "awaiting_exercises"
STEP_DONE = "done"

_YES_WORDS = {"yes", "y", "done", "completed", "complete", "finished", "full"}
_PARTIAL_WORDS = {"partial", "partially", "half"}
_SKIP_WORDS = {"skip", "skipped", "no", "didn't", "didnt", "missed", "none"}

CANCEL_COMMANDS = {"/cancel", "cancel"}
SKIP_COMMANDS = {"/skip", "skip"}
DONE_COMMANDS = {"/done", "done"}

_COMPLETION_PROMPT = "Did you complete the workout? Reply yes / partial / skipped."
_SKIP_REASON_PROMPT = "What's the reason (tired, sick, no time, ...)? Or /skip to leave it blank."
_DURATION_PROMPT = "How long did it take (minutes)? Or /skip to use the planned duration."


_WORD_RE = re.compile(r"[a-z']+")


def parse_completion(text: str) -> str | None:
    # Whole-word matching, not substring: "gym was closed" must not match
    # the bare "y" in _YES_WORDS, nor "not sure" match the bare "no" in
    # _SKIP_WORDS (which "not" contains as a substring).
    words = set(_WORD_RE.findall((text or "").lower()))
    if words & _SKIP_WORDS:
        return "skipped"
    if words & _PARTIAL_WORDS:
        return "partial"
    if words & _YES_WORDS:
        return "yes"
    return None


_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minutes)?", re.IGNORECASE
)


def parse_duration_seconds(text: str) -> float | None:
    """Minutes by default (bare "55" → 55 min); hour units multiply by 60."""
    match = _DURATION_RE.search((text or "").strip())
    if not match:
        return None
    value = float(match[1])
    unit = (match[2] or "min").lower()
    return value * 3600 if unit.startswith("h") else value * 60


_SETSREPS_RE = re.compile(r"(\d+)\s*[xX×]\s*(\d+)")
_WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.IGNORECASE)
_RPE_RE = re.compile(r"rpe\D{0,3}(\d+(?:\.\d+)?)", re.IGNORECASE)
_RIR_RE = re.compile(r"rir\D{0,3}(\d+(?:\.\d+)?)", re.IGNORECASE)
_NO_PAIN_RE = re.compile(r"no\s+pain", re.IGNORECASE)
_PAIN_RE = re.compile(r"pain[:\s]*([^.,;]*)", re.IGNORECASE)


def default_exercises(plan_exercises: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The plan's exercises, reshaped into log_strength_session defaults."""
    out = []
    for ex in plan_exercises or []:
        out.append({
            "exercise_name": ex.get("exercise_name") or ex.get("name") or "Exercise",
            "machine": ex.get("machine"),
            "planned_sets": ex.get("planned_sets") or ex.get("sets"),
            "planned_reps": (
                ex.get("planned_reps")
                or (str(ex["reps"]) if ex.get("reps") is not None else None)
            ),
            "planned_weight_kg": ex.get("planned_weight_kg") or ex.get("weight_kg"),
        })
    return out


def parse_exercise_reply(text: str, default: dict[str, Any]) -> dict[str, Any]:
    """Merge free text onto a default exercise (from the plan), extracting
    sets/reps/weight/RPE/RIR/pain where recognisable; whatever's left over
    becomes ``notes``. ``/skip`` accepts the planned defaults as-is."""
    result = dict(default)
    text = (text or "").strip()
    if not text or text.lower() in SKIP_COMMANDS:
        result["status"] = "completed"
        result["completed"] = True
        return result

    consumed: list[str] = []
    m = _SETSREPS_RE.search(text)
    if m:
        result["sets"], result["reps"] = int(m[1]), int(m[2])
        consumed.append(m[0])
    m = _WEIGHT_RE.search(text)
    if m:
        result["weight_kg"] = float(m[1])
        consumed.append(m[0])
    m = _RPE_RE.search(text)
    if m:
        result["rpe"] = float(m[1])
        consumed.append(m[0])
    m = _RIR_RE.search(text)
    if m:
        result["rir"] = float(m[1])
        consumed.append(m[0])
    no_pain = _NO_PAIN_RE.search(text)
    if no_pain:
        result["pain_note"] = None
        consumed.append(no_pain[0])
    else:
        m = _PAIN_RE.search(text)
        if m:
            result["pain_note"] = m[1].strip() or "reported"
            consumed.append(m[0])

    leftover = text
    for chunk in consumed:
        leftover = leftover.replace(chunk, "")
    leftover = re.sub(r"\s+", " ", leftover).strip(" ,.-")
    if leftover:
        result["notes"] = f"{result['notes']}; {leftover}" if result.get("notes") else leftover

    result["status"] = "completed"
    result["completed"] = True
    return result


def _exercise_prompt(exercise: dict[str, Any], index: int, total: int) -> str:
    name = exercise.get("exercise_name", "Exercise")
    hints = []
    if exercise.get("planned_sets") or exercise.get("planned_reps"):
        hints.append(f"planned {exercise.get('planned_sets') or '?'}x{exercise.get('planned_reps') or '?'}")
    if exercise.get("planned_weight_kg"):
        hints.append(f"{exercise['planned_weight_kg']}kg")
    hint = f" ({', '.join(hints)})" if hints else ""
    return (
        f'Exercise {index}/{total}: {name}{hint} — sets/reps/weight/RPE/pain? '
        'e.g. "3x8 @60kg RPE7 no pain". /skip to accept planned, /done to finish logging.'
    )


def advance(plan: dict[str, Any], state: dict[str, Any], text: str) -> dict[str, Any]:
    """One turn of the conversation. Never raises on unparseable input —
    unclear replies are asked again instead of dropped.

    Returns a dict of state field updates (merge onto ``state``) plus
    ``reply`` (what to send back) and, once the flow is finished,
    ``finished: True`` (and ``cancelled: True`` for an abandoned flow).
    """
    text = (text or "").strip()
    low = text.lower()
    step = state.get("step", STEP_AWAITING_COMPLETION)

    if low in CANCEL_COMMANDS:
        return {"step": STEP_DONE, "finished": True, "cancelled": True,
                "reply": "Cancelled — nothing was logged."}

    if step == STEP_AWAITING_COMPLETION:
        completion = parse_completion(text)
        if completion is None:
            return {"step": step, "reply": f"Sorry, I didn't catch that. {_COMPLETION_PROMPT}"}
        if completion == "skipped":
            return {"step": STEP_AWAITING_SKIP_REASON, "completion_status": "skipped",
                    "reply": _SKIP_REASON_PROMPT}
        return {
            "step": STEP_AWAITING_DURATION, "completion_status": completion,
            "planned_exercises": default_exercises(plan.get("exercises")),
            "logged_exercises": [], "current_exercise_index": 0,
            "reply": _DURATION_PROMPT,
        }

    if step == STEP_AWAITING_SKIP_REASON:
        reason = None if low in SKIP_COMMANDS else (text or None)
        return {"step": STEP_DONE, "finished": True, "notes": reason, "reply": "Noted."}

    if step == STEP_AWAITING_DURATION:
        if low in SKIP_COMMANDS:
            duration_s = plan.get("estimated_duration_s")
        else:
            duration_s = parse_duration_seconds(text)
            if duration_s is None:
                return {"step": step, "reply": f"Sorry, I didn't catch a duration. {_DURATION_PROMPT}"}
        planned = state.get("planned_exercises") or []
        if not planned:
            return {"step": STEP_DONE, "finished": True, "duration_s": duration_s,
                    "reply": "No exercises were planned for this session — logging as-is."}
        return {
            "step": STEP_AWAITING_EXERCISES, "duration_s": duration_s,
            "current_exercise_index": 0,
            "reply": _exercise_prompt(planned[0], 1, len(planned)),
        }

    if step == STEP_AWAITING_EXERCISES:
        planned = state.get("planned_exercises") or []
        logged = list(state.get("logged_exercises") or [])
        idx = state.get("current_exercise_index", 0)
        current_default = planned[idx] if idx < len(planned) else {"exercise_name": "Exercise"}
        if low not in DONE_COMMANDS:
            logged.append(parse_exercise_reply(text, current_default))
        idx += 1
        if low in DONE_COMMANDS or idx >= len(planned):
            return {"step": STEP_DONE, "finished": True, "logged_exercises": logged,
                    "reply": "Got it — finishing up."}
        return {
            "step": STEP_AWAITING_EXERCISES, "logged_exercises": logged,
            "current_exercise_index": idx,
            "reply": _exercise_prompt(planned[idx], idx + 1, len(planned)),
        }

    return {"step": STEP_DONE, "finished": True, "reply": "This workout log is already complete."}


class WorkoutLogFlows:
    """DB-backed wrapper: persists conversation state and, once a flow
    finishes, logs the result via the same primitives ChatGPT's tools use
    (add_strength_session / update_training_plan / add_health_event)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def _dump(self, row: WorkoutLogFlow) -> dict[str, Any]:
        out = row.model_dump()
        raw = out.pop("exercises_json", None)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}  # a corrupt row must not break the flow read
        out["planned_exercises"] = data.get("planned", [])
        out["logged_exercises"] = data.get("logged", [])
        result_raw = out.pop("result_json", None)
        try:
            out["result"] = json.loads(result_raw) if result_raw else None
        except ValueError:
            out["result"] = None
        return out

    @staticmethod
    def _exercises_json(state: dict[str, Any]) -> str:
        return json.dumps({
            "planned": state.get("planned_exercises") or [],
            "logged": state.get("logged_exercises") or [],
        }, ensure_ascii=False)

    def start(
        self, plan_id: int, reminder_id: int | None = None,
        timezone: str = DEFAULT_TIMEZONE,
    ) -> dict[str, Any]:
        plan = self.db.get_training_plan(plan_id)
        if plan is None:
            raise ValueError(f"no training plan with id {plan_id}")
        with self.db.session() as s:
            open_flows = s.exec(
                select(WorkoutLogFlow).where(
                    WorkoutLogFlow.completed_at == None  # noqa: E711
                )
            ).all()
            existing = next((f for f in open_flows if f.plan_id == plan_id), None)
            if existing is not None:
                return {"flow_id": existing.id, "prompt": _COMPLETION_PROMPT, "reused": True}
            # Only one flow can be "active" (a single Telegram chat can only
            # be replying to one conversation at a time) — an open flow for
            # a different plan would otherwise be silently orphaned, never
            # routed to again and never finished.
            now = datetime.now(tz_utc.utc)
            for other in open_flows:
                other.completed_at = now
                other.updated_at = now
                s.add(other)
            row = WorkoutLogFlow(plan_id=plan_id, reminder_id=reminder_id, timezone=timezone)
            s.add(row)
            s.commit()
            s.refresh(row)
            return {"flow_id": row.id, "prompt": _COMPLETION_PROMPT, "reused": False}

    def get(self, flow_id: int) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.get(WorkoutLogFlow, flow_id)
        return self._dump(row) if row is not None else None

    def active_flow(self) -> dict[str, Any] | None:
        """The most recent open flow — this is a single-user bot, so at most
        one workout log is ever being collected at a time."""
        with self.db.session() as s:
            row = s.exec(
                select(WorkoutLogFlow)
                .where(WorkoutLogFlow.completed_at == None)  # noqa: E711
                .order_by(WorkoutLogFlow.id.desc())
            ).first()
        return self._dump(row) if row is not None else None

    def _finalize(
        self, plan_id: int, plan: dict[str, Any], state: dict[str, Any], outcome: dict[str, Any],
    ) -> dict[str, Any]:
        if outcome.get("cancelled"):
            return {"plan_id": plan_id, "status": plan.get("status"), "cancelled": True}

        if state.get("completion_status") == "skipped":
            reason = outcome.get("notes", state.get("notes"))
            self.db.update_training_plan(plan_id, status="skipped", skip_reason=reason)
            self.db.add_health_event(
                "workout_done", {"plan_id": plan_id, "status": "skipped", "reason": reason}
            )
            return {
                "plan_id": plan_id, "status": "skipped", "skip_reason": reason,
                "next_step": "Rest up — we'll adjust the next session.",
            }

        status = "done" if state.get("completion_status") == "yes" else "partially_done"
        duration_s = outcome.get("duration_s", state.get("duration_s"))
        logged_exercises = outcome.get("logged_exercises", state.get("logged_exercises") or [])

        strength_session_id = None
        if logged_exercises:
            session = self.db.add_strength_session(
                plan.get("day"), exercises=logged_exercises,
                session_name=plan.get("title"), duration_s=duration_s,
            )
            strength_session_id = session["id"]

        notes_parts = [
            ex.get("pain_note") or ex.get("notes")
            for ex in logged_exercises
            if ex.get("pain_note") or ex.get("notes")
        ]
        notes = "; ".join(dict.fromkeys(notes_parts)) or None  # de-duplicated, order preserved

        self.db.update_training_plan(
            plan_id, status=status, actual_duration_s=duration_s, feedback=notes,
        )
        self.db.add_health_event(
            "workout_done",
            {
                "plan_id": plan_id, "status": status,
                "strength_session_id": strength_session_id,
                "duration_s": duration_s, "exercises_logged": len(logged_exercises),
            },
        )
        return {
            "plan_id": plan_id, "status": status,
            "strength_session_id": strength_session_id,
            "duration_s": duration_s, "exercises_logged": len(logged_exercises),
            "notes": notes,
            "next_step": "Eat post-workout meal and log it.",
        }

    def handle_reply(self, flow_id: int, text: str) -> dict[str, Any]:
        with self.db.session() as s:
            row = s.get(WorkoutLogFlow, flow_id)
        if row is None or row.completed_at is not None:
            return {"reply": "No active workout log to continue.", "finished": True}

        plan_id = row.plan_id
        plan = self.db.get_training_plan(plan_id) or {}
        state = self._dump(row)
        outcome = advance(plan, state, text)
        merged = {**state, **outcome}

        with self.db.session() as s:
            row = s.get(WorkoutLogFlow, flow_id)
            row.step = outcome.get("step", row.step)
            if "completion_status" in outcome:
                row.completion_status = outcome["completion_status"]
            if "duration_s" in outcome:
                row.duration_s = outcome["duration_s"]
            if "current_exercise_index" in outcome:
                row.current_exercise_index = outcome["current_exercise_index"]
            if "notes" in outcome:
                row.notes = outcome["notes"]
            if "planned_exercises" in outcome or "logged_exercises" in outcome:
                row.exercises_json = self._exercises_json(merged)
            now = datetime.now(tz_utc.utc)
            if outcome.get("finished"):
                row.completed_at = now
            row.updated_at = now
            s.add(row)
            s.commit()

        if not outcome.get("finished"):
            return {"reply": outcome["reply"], "finished": False}

        result = self._finalize(plan_id, plan, merged, outcome)
        with self.db.session() as s:
            row = s.get(WorkoutLogFlow, flow_id)
            row.result_json = json.dumps(result, ensure_ascii=False, default=str)
            s.add(row)
            s.commit()
        return {"reply": outcome["reply"], "finished": True, "result": result}
