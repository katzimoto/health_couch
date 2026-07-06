"""LLM coach engine (OpenAI).

Two entry points share one system prompt built from the user's goals plus the
current analyzer report:

* :meth:`Coach.morning_plan` — a structured daily plan (3 priorities, 1 workout,
  1 recovery/appearance tip), pushed at 07:30.
* :meth:`Coach.chat` — conversational replies grounded in the latest data and the
  last N turns of memory.

Only *summaries* (the analyzer report) go to the API — never the raw database.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from openai import OpenAI

from .analysis import Analyzer
from .config import settings
from .database import Database, as_json
from .sync_policy import classify_freshness, minutes_since

log = logging.getLogger("garmin_coach.coach")

# Plain-language freshness notes injected into the report context so the model
# always has something concrete to say about how current the data is.
_FRESHNESS_NOTE = {
    "none": "No Garmin sync has ever run — use whatever Health Coach data exists "
            "(profile, logged meals, plans, events).",
    "fresh": "Garmin data is fresh (synced within the last ~90 minutes).",
    "cached": "Garmin sync is a little stale — using cached data from the last "
              "sync. Still usable; label it as cached.",
    "stale": "Garmin sync is stale (over a day old) — using the latest cached "
             "data. Keep going and label it as stale/cached, do not refuse.",
}

_SYSTEM_PROMPT = """\
You are a personal health and fitness coach embedded in the user's own data \
pipeline. You see summaries of their Garmin metrics (sleep, HRV, resting heart \
rate, stress, body battery, steps, workouts, weight, body composition) plus \
computed trends and flags.

The user's stated goals:
{goals}

How to coach:
- Be concrete, warm and concise. Talk like a knowledgeable friend, not a report.
- Ground every recommendation in the data provided. Reference specific numbers \
and trends when they justify your advice.
- Respect the flags: if recovery is poor (HRV down, resting HR up, high sleep \
debt, load spiking), steer toward rest even if it conflicts with the plan.
- Prefer small, sustainable actions over heroic ones.
- These are general wellness suggestions, not medical advice. If you see signs \
that warrant a doctor (e.g. persistent large resting-HR jumps), say so plainly.

Data freshness and availability:
- You are given a `data_freshness` block. ALWAYS open with a one-line data \
status (fresh / cached / stale, and roughly how old). If a Garmin sync is \
stale or failed, keep going with the most recent cached Health Coach data and \
label it as cached — never refuse or fall back to a generic plan just because \
a Garmin sync is old or failed.
- Only say data is unavailable when there is genuinely nothing to work with \
(`has_usable_data` is false). Otherwise personalise from whatever is present: \
profile and goals, logged meals and nutrition totals, training plans, health \
events, hydration, and the cached Garmin-derived metrics.
"""

_MORNING_INSTRUCTION = """\
Write today's morning briefing. Use this exact structure and keep it tight \
(a Telegram message, so short lines, minimal markdown):

☀️ Good morning! Here's your day:

📡 Data: <one line: fresh / cached / stale, from data_freshness>

🎯 Top 3 priorities:
1. ...
2. ...
3. ...

🏋️ Today's workout: <one specific session, or an explicit rest day and why>

✨ Recovery / appearance tip: <one actionable tip toward looking and feeling better>

Across the priorities cover: a walking/cardio step target, nutrition targets \
(calories/protein from the profile) and a hydration target. Base every line on \
the data and flags. If the data says rest, prescribe rest. If Garmin is \
stale/failed, use the cached data and say so — never a generic plan.
"""

_MORNING_INSTRUCTION_STRUCTURED = """\
Produce today's morning briefing from the data provided. Requirements:
- Priority 1 must open with the data-freshness status (fresh / cached / stale, \
from `data_freshness`).
- Exactly 3 priorities. Across them cover a walking/cardio step target, \
nutrition targets (calories/protein from the profile) and a hydration target.
- One workout: a single specific session (the train-or-recover call), or an \
explicit rest day with the reason in `details` and `is_rest_day` true.
- One actionable recovery/appearance tip (the day's single behaviour focus).
Base every field on the data and flags; if the data says rest, prescribe rest. \
If Garmin is stale/failed, use the cached data and label it — never a generic \
plan. Keep each string short — this renders into a Telegram message.
"""

_EVENING_INSTRUCTION = """\
Write today's evening report. Use this structure and keep it tight (a Telegram
message, so short lines, minimal markdown):

🌙 Evening report

📡 Data: <one line: fresh / cached / stale, from data_freshness>
📋 Plan vs actual: how today went against the morning plan (use the plan,
training-plan status and logged events)
🏋️ Training: workouts / training completion vs plan
👣 Steps & cardio: the day's numbers
🍽 Meals & macros: meals logged; calories/macros vs targets; protein, fiber,
vegetables — call out skipped or unlogged meals
💧 Hydration: intake vs target
🛌 Sleep / recovery: anything in the data worth noting for tomorrow

✅ One thing that went well
🔁 What needs improvement
🎯 Top 1–3 concrete changes for tomorrow

Base every line on the data provided. If meal logs are missing, say the meal
logs are incomplete — do NOT say all data is unavailable when Garmin/Health
Coach has other usable data. If Garmin is stale/failed, use the cached data and
label it. If something wasn't logged, say so plainly instead of guessing.
"""


# Strict json_schema keeps the plan machine-readable (per-item adherence
# tracking) and immune to format drift. Only strict-mode-supported keywords
# here — "exactly 3 priorities" is enforced by the instruction, not minItems.
_PLAN_SCHEMA = {
    "name": "morning_plan",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "priorities": {
                "type": "array",
                "description": "Exactly 3 short, concrete priorities for today.",
                "items": {"type": "string"},
            },
            "workout": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "details": {"type": "string"},
                    "is_rest_day": {"type": "boolean"},
                },
                "required": ["title", "details", "is_rest_day"],
                "additionalProperties": False,
            },
            "recovery_tip": {"type": "string"},
        },
        "required": ["priorities", "workout", "recovery_tip"],
        "additionalProperties": False,
    },
}


def _render_plan(data: dict[str, Any]) -> str:
    """Structured plan → the Telegram text the user has always received."""
    lines = ["☀️ Good morning! Here's your day:", "", "🎯 Top 3 priorities:"]
    for i, priority in enumerate((data.get("priorities") or [])[:3], 1):
        lines.append(f"{i}. {priority}")
    workout = data.get("workout") or {}
    workout_text = workout.get("title") or "Rest day"
    if workout.get("details"):
        workout_text += f" — {workout['details']}"
    lines += [
        "",
        f"🏋️ Today's workout: {workout_text}",
        "",
        f"✨ Recovery / appearance tip: {data.get('recovery_tip', '')}",
    ]
    return "\n".join(lines)


def _has_usable_data(context: dict[str, Any]) -> bool:
    """True when there is *any* Health Coach data to personalise from — so the
    report never falls back to a generic plan while local data exists, even if
    Garmin sync is stale or has never run."""
    if context.get("profile"):
        return True
    if (context.get("analysis") or {}).get("available"):
        return True
    if context.get("training_plans_today"):
        return True
    if context.get("health_events_today"):
        return True
    if context.get("hydration_today"):
        return True
    if context.get("recent_feedback"):
        return True
    if context.get("recent_meals"):
        return True
    nutrition = context.get("nutrition_today") or []
    if any((day.get("meal_count") or 0) > 0 for day in nutrition):
        return True
    return False


class Coach:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()
        self.analyzer = Analyzer(self.db)
        self._client: OpenAI | None = None

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            if not settings.openai_api_key:
                raise RuntimeError("OPENAI_API_KEY is not set.")
            self._client = OpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url or None,
            )
        return self._client

    def _system_prompt(self) -> str:
        return _SYSTEM_PROMPT.format(goals=settings.coach_goals)

    def _data_context(self) -> str:
        """The analyzer report plus recent feedback, as a data block."""
        report = self.analyzer.report()
        feedback = self.db.recent_feedback(days=7)
        context = {"analysis": report, "recent_feedback": feedback}
        return as_json(context)

    def _data_freshness(self) -> dict[str, Any]:
        """How current the cached Garmin data is — a local read only, never a
        network sync. Classifies the last pull as none/fresh/cached/stale so
        every report can state its data status."""
        last = self.db.last_pull()
        ts = last["ts"] if last else None
        minutes = minutes_since(ts)
        status = classify_freshness(minutes)
        return {
            "status": status,  # none | fresh | cached | stale
            "using_cached_data": status in ("cached", "stale"),
            "last_synced_day": last["day"] if last else None,
            "last_synced_at": ts.isoformat() if ts else None,
            "minutes_since_last_sync": minutes,
            "note": _FRESHNESS_NOTE[status],
        }

    def get_health_context_for_report(self, mode: str = "morning") -> dict[str, Any]:
        """Read all locally-available Health Coach data for a plan/report.

        Reads ONLY the local database — profile, analyzer summaries (cached
        Garmin-derived metrics), today's nutrition/hydration/plans/health
        events, and recent feedback. It never triggers a Garmin network sync:
        sync is an optional refresh step the caller does separately (and which
        is throttled), not a prerequisite for producing a report. The
        ``data_freshness`` block says how current the cached Garmin data is,
        and ``has_usable_data`` says whether there is anything to personalise
        from at all.
        """
        today = date.today().isoformat()
        context: dict[str, Any] = {
            "data_freshness": self._data_freshness(),
            "profile": self.db.get_profile(),
            "analysis": self.analyzer.report(),
            "training_plans_today": self.db.get_today_training_plans(),
            "nutrition_today": self.db.nutrition_summary(day=today),
            "hydration_today": self.db.recent_hydration(days=1),
            "health_events_today": self.db.health_events_for_day(today),
        }
        if mode == "evening":
            last_plan = self.db.last_plan()
            context["todays_morning_plan"] = (
                last_plan if last_plan and last_plan.get("day") == today else None
            )
            context["recent_feedback"] = self.db.recent_feedback(days=1)
        else:
            context["recent_meals"] = self.db.recent_meals(days=3)
            context["recent_feedback"] = self.db.recent_feedback(days=7)
        context["has_usable_data"] = _has_usable_data(context)
        return context

    def _complete(self, messages: list[dict[str, str]], max_tokens: int = 600) -> str:
        resp = self.client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            temperature=0.6,
            max_tokens=max_tokens,
        )
        return (resp.choices[0].message.content or "").strip()

    def _complete_structured_plan(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        """Strict-schema completion for the plan. Raises on refusal/invalid
        JSON/unsupported provider so the caller can fall back to free text."""
        resp = self.client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            temperature=0.6,
            max_tokens=500,
            response_format={"type": "json_schema", "json_schema": _PLAN_SCHEMA},
        )
        message = resp.choices[0].message
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise RuntimeError(f"Model refused the structured plan: {refusal}")
        return json.loads(message.content or "")

    # ── Public API ─────────────────────────────────────────────────────────────

    def morning_plan(self, reuse_today: bool = False) -> str:
        """Generate and persist today's structured plan.

        With ``reuse_today``, return the already-saved plan if one exists for
        today instead of generating again — used by the scheduler's retry path
        so a failed *send* doesn't pay for (and re-seed memory with) a second
        generation.
        """
        if reuse_today:
            existing = self.db.last_plan()
            if existing and existing.get("day") == date.today().isoformat():
                log.info("Reusing today's saved morning plan.")
                return existing["plan"]

        context = self.get_health_context_for_report(mode="morning")

        def messages_for(instruction: str) -> list[dict[str, str]]:
            return [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": (
                        "Here is my current Health Coach data and trends:\n\n"
                        f"{as_json(context)}\n\n{instruction}"
                    ),
                },
            ]

        details: dict[str, Any] | None = None
        try:
            details = self._complete_structured_plan(
                messages_for(_MORNING_INSTRUCTION_STRUCTURED)
            )
            plan = _render_plan(details)
        except Exception:  # noqa: BLE001 — provider/model may not support json_schema
            log.warning(
                "Structured plan failed — falling back to free text.", exc_info=True
            )
            plan = self._complete(messages_for(_MORNING_INSTRUCTION), max_tokens=500)
        self.db.save_plan(date.today(), plan, details=details)
        # Seed conversation memory so follow-up chat has today's plan in context.
        self.db.add_message("assistant", plan)
        log.info("Generated morning plan (%d chars).", len(plan))
        return plan

    def evening_report(self) -> str:
        """Generate the plan-vs-actual evening report (the /report command).

        Grounded in the analyzer report plus today's *user-logged* material:
        nutrition totals, training-plan adherence, hydration and the
        structured Telegram health events (meals logged/skipped, water,
        workouts done). Like recent_feedback in chat, these are the user's own
        log entries — raw Garmin rows still never leave the analyzer.
        """
        context = self.get_health_context_for_report(mode="evening")
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {
                "role": "user",
                "content": (
                    "Here is my health data, today's plan and what I logged "
                    f"today:\n\n{as_json(context)}\n\n{_EVENING_INSTRUCTION}"
                ),
            },
        ]
        report = self._complete(messages, max_tokens=600)
        # Seed conversation memory so follow-up chat can discuss the report.
        self.db.add_message("assistant", report)
        log.info("Generated evening report (%d chars).", len(report))
        return report

    def chat(self, user_message: str) -> str:
        """Answer a free-text question grounded in data + conversation memory."""
        self.db.add_message("user", user_message)
        history = self.db.recent_messages(limit=16)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self._system_prompt()},
            {
                "role": "system",
                "content": f"Current data snapshot:\n{self._data_context()}",
            },
            *history,
        ]
        reply = self._complete(messages, max_tokens=500)
        self.db.add_message("assistant", reply)
        return reply

    def analysis_snapshot(self) -> dict[str, Any]:
        """Expose the raw analyzer report (used by /status and tests)."""
        return self.analyzer.report()
