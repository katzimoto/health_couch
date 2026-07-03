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
            self._client = OpenAI(api_key=settings.openai_api_key)
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

    # ── Public API ─────────────────────────────────────────────────────────────

    def morning_plan(self) -> str:
        """Generate and persist today's structured plan."""
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {
                "role": "user",
                "content": (
                    "Here is my current health data and trends:\n\n"
                    f"{self._data_context()}\n\n{_MORNING_INSTRUCTION}"
                ),
            },
        ]
        plan = self._complete(messages, max_tokens=500)
        self.db.save_plan(date.today(), plan)
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
