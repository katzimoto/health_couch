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

log = logging.getLogger("garmin_coach.coach")

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
"""

_MORNING_INSTRUCTION = """\
Write today's morning briefing. Use this exact structure and keep it tight \
(a Telegram message, so short lines, minimal markdown):

☀️ Good morning! Here's your day:

🎯 Top 3 priorities:
1. ...
2. ...
3. ...

🏋️ Today's workout: <one specific session, or an explicit rest day and why>

✨ Recovery / appearance tip: <one actionable tip toward looking and feeling better>

Base every line on the data and flags. If the data says rest, prescribe rest.
"""

_MORNING_INSTRUCTION_STRUCTURED = """\
Produce today's morning briefing: exactly 3 priorities, one workout (a single \
specific session — or an explicit rest day with the reason in `details` and \
`is_rest_day` true), and one actionable recovery/appearance tip. Base every \
field on the data and flags; if the data says rest, prescribe rest. Keep each \
string short — this renders into a Telegram message.
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

        def messages_for(instruction: str) -> list[dict[str, str]]:
            return [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": (
                        "Here is my current health data and trends:\n\n"
                        f"{self._data_context()}\n\n{instruction}"
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
