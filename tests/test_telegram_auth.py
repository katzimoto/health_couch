"""Offline tests for the Telegram bot's chat-id authorization guard.

``_authorized`` is the security boundary that keeps a leaked bot handle from
exposing health data or spending API credits — it deserves direct coverage.
Updates are duck-typed stand-ins; no Telegram network involved.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from garmin_coach.storage.database import Database
from garmin_coach.surfaces.telegram_bot import TelegramCoach


def _update(chat_id=None, no_chat: bool = False):
    chat = None if no_chat else SimpleNamespace(id=chat_id)
    return SimpleNamespace(effective_chat=chat, message=None)


@pytest.fixture()
def bot(tmp_path) -> TelegramCoach:
    bot = TelegramCoach(Database(path=str(tmp_path / "tg.db")))
    bot._allowed = "12345"  # pin regardless of the test environment's .env
    return bot


def test_matching_chat_id_is_authorized(bot: TelegramCoach) -> None:
    assert bot._authorized(_update(chat_id=12345)) is True


def test_other_chat_id_is_rejected(bot: TelegramCoach) -> None:
    assert bot._authorized(_update(chat_id=99999)) is False


def test_update_without_chat_is_rejected(bot: TelegramCoach) -> None:
    assert bot._authorized(_update(no_chat=True)) is False


def test_no_configured_chat_id_allows_all(bot: TelegramCoach) -> None:
    bot._allowed = ""
    assert bot._authorized(_update(chat_id=99999)) is True
