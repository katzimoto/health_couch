"""Two-way Telegram coach (long polling — no public endpoint needed).

Commands:
  /start   — greeting + how it works
  /plan    — generate and send today's morning plan on demand
  /status  — quick data snapshot (latest metrics + active flags)
  /done, /skipped, /felt <text> — log feedback that shapes tomorrow's plan

Any other text message is treated as a question for the coach and answered via
the OpenAI engine, grounded in the latest data and conversation memory.

Access is restricted to ``TELEGRAM_CHAT_ID`` so a leaked bot handle can't let a
stranger read your health data or spend your API credits.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .coach import Coach
from .config import settings
from .database import Database
from .heartbeat import beat

log = logging.getLogger("garmin_coach.telegram")

_HEARTBEAT_INTERVAL_S = 60


class TelegramCoach:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()
        self.coach = Coach(self.db)
        self._allowed = settings.telegram_chat_id.strip()

    # ── Guards ─────────────────────────────────────────────────────────────────

    def _authorized(self, update: Update) -> bool:
        if not self._allowed:
            return True  # no restriction configured
        chat = update.effective_chat
        return chat is not None and str(chat.id) == self._allowed

    async def _deny(self, update: Update) -> None:
        log.warning("Rejected message from unauthorized chat %s", update.effective_chat)
        if update.message:
            await update.message.reply_text("Not authorized.")

    # ── Handlers ───────────────────────────────────────────────────────────────

    async def start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        await update.message.reply_text(
            "👋 I'm your health coach.\n\n"
            "I read your Garmin data every day and can help you train, recover "
            "and hit your goals.\n\n"
            "• /plan — today's plan\n"
            "• /status — your latest numbers\n"
            "• /done, /skipped, /felt <note> — log how it went\n"
            "• …or just message me a question anytime."
        )

    async def plan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            # to_thread: the OpenAI call takes seconds and would otherwise
            # freeze polling and every other handler for its whole duration.
            text = await asyncio.to_thread(self.coach.morning_plan)
        except Exception:  # noqa: BLE001
            log.exception("Plan generation failed")
            # Exception text can leak internals (URLs, key fragments) — keep
            # the details in the log.
            text = "⚠️ Couldn't generate a plan right now. Check the server logs."
        await update.message.reply_text(text)

    async def status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        report = await asyncio.to_thread(self.coach.analysis_snapshot)
        if not report.get("available"):
            await update.message.reply_text(
                "No data yet — run a Garmin pull/backfill first."
            )
            return
        latest = report.get("latest", {})

        def fmt(key: str, label: str, unit: str = "") -> str | None:
            val = latest.get(key)
            return f"• {label}: {val}{unit}" if val is not None else None

        lines = [f"📊 As of {report.get('as_of')}:"]
        lines += [
            x for x in (
                fmt("sleep_hours", "Sleep", " h"),
                fmt("sleep_score", "Sleep score"),
                fmt("hrv", "HRV", " ms"),
                fmt("resting_hr", "Resting HR", " bpm"),
                fmt("steps", "Steps"),
                fmt("weight_kg", "Weight", " kg"),
                fmt("body_fat", "Body fat", " %"),
            ) if x
        ]
        flags = report.get("flags", [])
        if flags:
            lines.append("\n🚩 Flags:")
            lines += [f"• {f}" for f in flags]
        else:
            lines.append("\n✅ No flags — you're in good shape.")
        await update.message.reply_text("\n".join(lines))

    async def feedback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        command = (update.message.text or "").split()[0].lstrip("/").split("@")[0]
        extra = " ".join(ctx.args) if ctx.args else ""
        note = {
            "done": "Completed today's plan.",
            "skipped": "Skipped today's plan.",
        }.get(command, extra or "felt: (no detail)")
        if command == "felt":
            note = f"Felt: {extra}" if extra else "Felt: (no detail)"
        await asyncio.to_thread(self.db.add_feedback, note)
        await update.message.reply_text("Logged 👍 — I'll factor that into tomorrow.")

    async def message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        text = (update.message.text or "").strip()
        if not text:
            return
        await update.effective_chat.send_action(ChatAction.TYPING)
        try:
            reply = await asyncio.to_thread(self.coach.chat, text)
        except Exception:  # noqa: BLE001
            log.exception("Chat failed")
            reply = "⚠️ Something went wrong. Check the server logs."
        await update.message.reply_text(reply)

    # ── Push + run ─────────────────────────────────────────────────────────────

    async def push_morning_plan(self, app: Application | None = None) -> None:
        """Generate today's plan and push it to the configured chat.

        Called by the scheduler. Builds its own short-lived Application if one
        isn't supplied.
        """
        if not self._allowed:
            log.error("TELEGRAM_CHAT_ID not set — cannot push morning plan.")
            return
        # reuse_today: on a retry after a failed *send*, resend the plan that
        # was already generated and saved instead of paying for a new one.
        text = await asyncio.to_thread(self.coach.morning_plan, reuse_today=True)
        owns_app = app is None
        if owns_app:
            app = Application.builder().token(settings.telegram_bot_token).build()
            await app.initialize()
        try:
            await app.bot.send_message(chat_id=int(self._allowed), text=text)
            log.info("Pushed morning plan to chat %s", self._allowed)
        finally:
            if owns_app:
                await app.shutdown()

    async def _heartbeat_loop(self) -> None:
        """Touch the liveness file as long as the event loop is responsive."""
        while True:
            beat("telegram")
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)

    async def _post_init(self, _app: Application) -> None:
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    def build_app(self) -> Application:
        app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .post_init(self._post_init)
            .build()
        )
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("plan", self.plan))
        app.add_handler(CommandHandler("status", self.status))
        for cmd in ("done", "skipped", "felt"):
            app.add_handler(CommandHandler(cmd, self.feedback))
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.message)
        )
        return app

    def run(self) -> None:
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set.")
        log.info("Starting Telegram coach (long polling).")
        self.build_app().run_polling(allowed_updates=Update.ALL_TYPES)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    TelegramCoach().run()


if __name__ == "__main__":
    main()
