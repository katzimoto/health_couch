"""Offline tests for the coach's structured morning plan (fake OpenAI client)."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from garmin_coach.coach import Coach, _render_plan
from garmin_coach.database import Database

_PLAN = {
    "priorities": ["Drink 2L water", "Walk 8k steps", "In bed by 22:30"],
    "workout": {"title": "Zone 2 run", "details": "40 min easy", "is_rest_day": False},
    "recovery_tip": "10 min of stretching after the run.",
}


def _response(content: str | None, refusal: str | None = None):
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    """Yields queued responses; a queued Exception is raised instead."""

    def __init__(self, responses: list) -> None:
        self._responses = responses
        self.calls: list[dict] = []
        completions = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(completions=completions)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def coach(tmp_path) -> Coach:
    return Coach(Database(path=str(tmp_path / "coach.db")))


def test_structured_plan_renders_and_persists_details(coach: Coach) -> None:
    fake = FakeClient([_response(json.dumps(_PLAN))])
    coach._client = fake

    text = coach.morning_plan()

    assert fake.calls[0]["response_format"]["type"] == "json_schema"
    assert "1. Drink 2L water" in text
    assert "🏋️ Today's workout: Zone 2 run — 40 min easy" in text
    saved = coach.db.last_plan()
    assert saved["day"] == date.today().isoformat()
    assert saved["plan"] == text
    assert saved["details"]["workout"]["is_rest_day"] is False


def test_falls_back_to_free_text_when_schema_unsupported(coach: Coach) -> None:
    # e.g. a provider/model without json_schema support raises on the first
    # call; the retry without response_format must still produce a plan.
    fake = FakeClient([RuntimeError("response_format not supported"),
                       _response("☀️ fallback plan text")])
    coach._client = fake

    text = coach.morning_plan()

    assert text == "☀️ fallback plan text"
    assert "response_format" not in fake.calls[1]
    saved = coach.db.last_plan()
    assert saved["plan"] == text
    assert "details" not in saved  # nothing structured to store


def test_refusal_is_treated_as_failure_not_plan(coach: Coach) -> None:
    fake = FakeClient([_response(None, refusal="I can't help with that."),
                       _response("plain plan")])
    coach._client = fake
    assert coach.morning_plan() == "plain plan"


def test_reuse_today_returns_saved_plan_without_api_call(coach: Coach) -> None:
    coach.db.save_plan(date.today(), "saved plan", details=_PLAN)
    coach._client = FakeClient([])  # any API call would pop an empty list
    assert coach.morning_plan(reuse_today=True) == "saved plan"


def test_render_plan_handles_rest_day() -> None:
    text = _render_plan(
        {
            "priorities": ["a", "b", "c"],
            "workout": {"title": "Rest day", "details": "HRV still low", "is_rest_day": True},
            "recovery_tip": "sleep",
        }
    )
    assert "🏋️ Today's workout: Rest day — HRV still low" in text
