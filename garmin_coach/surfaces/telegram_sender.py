"""Minimal synchronous Telegram Bot API sender.

The reminder dispatch loop and the MCP ``send_telegram_message_now`` tool both
need a one-shot "send this text to the owner" primitive. Spinning up the async
python-telegram-bot ``Application`` for that is overkill (and awkward from the
sync MCP tools), so this talks to the Bot HTTP API directly with httpx. The
interactive bot in :mod:`telegram_bot` keeps using python-telegram-bot.
"""

from __future__ import annotations

import httpx

from garmin_coach.config import settings

_API_BASE = "https://api.telegram.org"
_TIMEOUT_S = 30


def send_telegram_message(
    text: str, chat_id: str | None = None, token: str | None = None
) -> int:
    """Send ``text`` to the configured chat; return Telegram's message_id.

    Raises ``RuntimeError`` on missing configuration or a rejected send so
    callers can record the failure (delivery log) instead of losing it.
    """
    token = token or settings.telegram_bot_token
    chat = chat_id or settings.telegram_chat_id
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
    if not chat:
        raise RuntimeError("TELEGRAM_CHAT_ID is not set.")
    response = httpx.post(
        f"{_API_BASE}/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text},
        timeout=_TIMEOUT_S,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Telegram send failed: HTTP {response.status_code} (non-JSON reply)"
        ) from exc
    if not data.get("ok"):
        # data["description"] is Telegram's error text, safe to surface.
        raise RuntimeError(
            f"Telegram send failed: {data.get('description') or response.status_code}"
        )
    return int(data["result"]["message_id"])
