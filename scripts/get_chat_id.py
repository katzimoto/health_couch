"""Discover your Telegram chat id.

Message your bot at least once (send it "hi"), then run:

    docker compose run --rm telegram python scripts/get_chat_id.py

It calls getUpdates and prints the chat id(s) that have messaged the bot. Put the
value into TELEGRAM_CHAT_ID in your .env so the coach only talks to you.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from garmin_coach.config import settings  # noqa: E402


def main() -> int:
    token = settings.telegram_bot_token
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN is not set in your environment/.env.", file=sys.stderr)
        return 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        resp = httpx.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Request failed: {exc}", file=sys.stderr)
        return 1

    updates = resp.json().get("result", [])
    if not updates:
        print(
            "No updates found. Send your bot a message first (e.g. 'hi'), then "
            "re-run this script."
        )
        return 0

    seen: dict[int, str] = {}
    for upd in updates:
        chat = (
            upd.get("message", {}).get("chat")
            or upd.get("edited_message", {}).get("chat")
            or {}
        )
        if chat.get("id") is not None:
            name = chat.get("username") or chat.get("first_name") or chat.get("title", "")
            seen[chat["id"]] = name

    print("Chats that have messaged your bot:")
    for chat_id, name in seen.items():
        print(f"  TELEGRAM_CHAT_ID={chat_id}   ({name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
