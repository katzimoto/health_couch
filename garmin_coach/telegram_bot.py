"""Two-way Telegram coach (long polling — no public endpoint needed).

Commands:
  /start   — greeting + how it works
  /plan    — generate and send today's morning plan on demand
  /report  — generate today's evening-style report on demand
  /status  — quick data snapshot (latest metrics + active flags)
  /done [workout], /skipped [lunch|dinner|…], /felt <text> — log how it went
  /meal <description> — log a meal (a photo message also logs as a meal)
  /water <ml> — add to today's hydration total
  /reminders, /edit_reminder, /pause_reminder, /resume_reminder,
  /delete_reminder — manage the scheduled Telegram reminders

Replies to reminders (meals, water, skips, workout completions) are stored as
structured rows in the health_event table, so ChatGPT/the coach can read back
what actually happened when writing plans and reports.

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
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .coach import Coach
from .config import settings
from .database import Database
from .heartbeat import beat
from .reminders import Reminders
from .workout_flow import WorkoutLogFlows

log = logging.getLogger("garmin_coach.telegram")

_HEARTBEAT_INTERVAL_S = 60


class TelegramCoach:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()
        self.coach = Coach(self.db)
        self.reminders = Reminders(self.db)
        self.workout_flows = WorkoutLogFlows(self.db)
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
            "• /report — today's evening report\n"
            "• /status — your latest numbers\n"
            "• /meal <description> — log a meal (or just send a photo)\n"
            "• /water <ml> — log water\n"
            "• /done [workout], /skipped [lunch|dinner], /felt <note> — log how it went\n"
            "• /reminders — your scheduled reminders\n"
            "• …or just message me a question anytime."
        )

    async def plan(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        await update.effective_chat.send_action(ChatAction.TYPING)
        await asyncio.to_thread(self.db.add_health_event, "plan_request", {})
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

    _MEAL_NAMES = {"breakfast", "lunch", "dinner", "snack"}

    async def feedback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        command = (update.message.text or "").split()[0].lstrip("/").split("@")[0]
        args = ctx.args or []
        extra = " ".join(args)
        # "/skipped lunch" is a structured skipped-meal event, not plan feedback.
        if command == "skipped" and args and args[0].lower() in self._MEAL_NAMES:
            meal = args[0].lower()
            await asyncio.to_thread(self._log_skipped_meal, meal)
            await update.message.reply_text(f"Noted — {meal} skipped. 📝")
            return
        # "/done workout" completes today's planned workout (if any).
        if command == "done" and args and args[0].lower() == "workout":
            reply = await asyncio.to_thread(
                self._log_workout_done, " ".join(args[1:]).strip()
            )
            await update.message.reply_text(reply)
            return
        note = {
            "done": "Completed today's plan.",
            "skipped": "Skipped today's plan.",
        }.get(command, extra or "felt: (no detail)")
        if command == "felt":
            note = f"Felt: {extra}" if extra else "Felt: (no detail)"
        await asyncio.to_thread(self.db.add_feedback, note)
        await update.message.reply_text("Logged 👍 — I'll factor that into tomorrow.")

    def _log_skipped_meal(self, meal: str) -> None:
        self.db.add_health_event("skipped_meal", {"meal": meal})
        self.db.add_feedback(f"Skipped {meal}.")

    def _log_workout_done(self, note: str) -> str:
        """Mark today's first still-planned workout done, or record a simple
        completion when nothing was planned."""
        open_plans = [
            p for p in self.db.get_today_training_plans() if p["status"] == "planned"
        ]
        if open_plans:
            plan = open_plans[0]
            self.db.update_training_plan(plan["id"], status="done", feedback=note or None)
            self.db.add_health_event(
                "workout_done", {"plan_id": plan["id"], "note": note or None}
            )
            title = plan.get("title") or "today's workout"
            return f"💪 Marked “{title}” done."
        self.db.add_health_event("workout_done", {"note": note or None})
        self.db.add_feedback("Completed a workout." + (f" {note}" if note else ""))
        return "💪 Workout logged (nothing was planned for today)."

    # ── Meals / water ──────────────────────────────────────────────────────────

    async def meal(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        text = " ".join(ctx.args).strip() if ctx.args else ""
        if not text:
            await update.message.reply_text(
                "Usage: /meal <description> — or send a photo (with an "
                "optional caption) and I'll log it as a meal."
            )
            return
        await asyncio.to_thread(self._log_meal, text, False)
        await update.message.reply_text(
            "🍽 Logged. Tell ChatGPT about it later for macro estimates, or "
            "/skipped lunch|dinner if a meal didn't happen."
        )

    async def photo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """A photo message is treated as a meal photo (the main reason this
        single-user bot ever receives one)."""
        if not self._authorized(update):
            return await self._deny(update)
        caption = (update.message.caption or "").strip()
        await asyncio.to_thread(self._log_meal, caption or "Meal (photo)", True)
        await update.message.reply_text(
            "📸 Logged as a meal"
            + (f": {caption}" if caption else " — add a caption next time for a better record.")
        )

    def _log_meal(self, description: str, photo: bool) -> None:
        """Store the meal both as a Meal row (so nutrition summaries see it —
        macros can be filled in later via ChatGPT's update_meal) and as a
        structured health event (so reports know it came in via Telegram)."""
        self.db.add_meal(name=description, note="via Telegram")
        self.db.add_health_event("meal", {"text": description, "photo": photo})

    async def water(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        raw = (ctx.args[0] if ctx.args else "").lower().removesuffix("ml")
        try:
            ml = int(raw)
            if not 0 < ml <= 5000:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Usage: /water <ml> — e.g. /water 500")
            return
        total = await asyncio.to_thread(self._log_water, ml)
        await update.message.reply_text(f"💧 +{ml} ml — {total} ml today.")

    def _log_water(self, ml: int) -> int:
        total = self.db.add_hydration_intake(ml)
        self.db.add_health_event("hydration", {"added_ml": ml, "total_ml": total})
        return total

    # ── Evening report ─────────────────────────────────────────────────────────

    async def report(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        await update.effective_chat.send_action(ChatAction.TYPING)
        await asyncio.to_thread(self.db.add_health_event, "report_request", {})
        try:
            text = await asyncio.to_thread(self.coach.evening_report)
        except Exception:  # noqa: BLE001
            log.exception("Evening report failed")
            text = "⚠️ Couldn't generate a report right now. Check the server logs."
        await update.message.reply_text(text)

    # ── Reminder management ────────────────────────────────────────────────────

    async def reminders_list(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        rows = await asyncio.to_thread(self.reminders.list)
        if not rows:
            await update.message.reply_text(
                "No reminders yet. Ask ChatGPT to create some, or install the "
                "defaults via its create_default_health_reminders tool."
            )
            return
        lines = ["⏰ Reminders:"]
        for r in rows:
            state = "▶️" if r["enabled"] else "⏸ paused"
            lines.append(
                f"#{r['id']} {r['title']} — {r['time']} {r['recurrence']} "
                f"({r['timezone']}) {state}"
            )
        lines.append(
            "\n/edit_reminder <id> <field> <value> · /pause_reminder <id> · "
            "/resume_reminder <id> · /delete_reminder <id>"
        )
        await update.message.reply_text("\n".join(lines))

    @staticmethod
    def _reminder_id(ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
        try:
            return int(ctx.args[0])
        except (IndexError, TypeError, ValueError):
            return None

    async def reminder_admin(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/pause_reminder, /resume_reminder and /delete_reminder <id>."""
        if not self._authorized(update):
            return await self._deny(update)
        command = (update.message.text or "").split()[0].lstrip("/").split("@")[0]
        rid = self._reminder_id(ctx)
        if rid is None:
            await update.message.reply_text(f"Usage: /{command} <id> — see /reminders for ids.")
            return
        if command == "delete_reminder":
            deleted = await asyncio.to_thread(self.reminders.delete, rid)
            await update.message.reply_text(
                f"🗑 Reminder #{rid} deleted — it won't fire again."
                if deleted else f"No reminder #{rid}."
            )
            return
        enabled = command == "resume_reminder"
        updated = await asyncio.to_thread(self.reminders.set_enabled, rid, enabled)
        if updated is None:
            await update.message.reply_text(f"No reminder #{rid}.")
        elif enabled:
            await update.message.reply_text(
                f"▶️ Reminder #{rid} resumed — next: {updated['next_run_at']} UTC."
            )
        else:
            await update.message.reply_text(f"⏸ Reminder #{rid} paused.")

    _EDITABLE_REMINDER_FIELDS = ("title", "message", "time", "timezone", "recurrence", "date")

    async def edit_reminder(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return await self._deny(update)
        args = ctx.args or []
        rid = self._reminder_id(ctx)
        if rid is None or len(args) < 3 or args[1].lower() not in self._EDITABLE_REMINDER_FIELDS:
            await update.message.reply_text(
                "Usage: /edit_reminder <id> <field> <value>\n"
                f"Fields: {', '.join(self._EDITABLE_REMINDER_FIELDS)}\n"
                "e.g. /edit_reminder 2 time 13:30\n"
                "(For bigger edits, ask ChatGPT — it can edit everything at once.)"
            )
            return
        field, value = args[1].lower(), " ".join(args[2:])
        try:
            updated = await asyncio.to_thread(self.reminders.edit, rid, **{field: value})
        except ValueError as exc:
            await update.message.reply_text(f"⚠️ {exc}")
            return
        if updated is None:
            await update.message.reply_text(f"No reminder #{rid}.")
            return
        await update.message.reply_text(
            f"✏️ Reminder #{rid} updated ({field} → {value}). "
            f"Next: {updated['next_run_at']} UTC."
        )

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

    # ── Workout-completion flow ────────────────────────────────────────────────

    @staticmethod
    def _format_flow_result(result: dict) -> str:
        if result.get("cancelled"):
            return "❎ Cancelled — nothing was logged."
        if result.get("status") == "skipped":
            reason = result.get("skip_reason")
            return "📝 Marked skipped." + (f" Reason: {reason}" if reason else "")
        lines = [f"✅ Workout logged — status: {result.get('status')}."]
        if result.get("duration_s"):
            lines.append(f"Duration: {round(result['duration_s'] / 60)} min")
        if result.get("exercises_logged"):
            lines.append(f"Exercises logged: {result['exercises_logged']}")
        if result.get("notes"):
            lines.append(f"Notes: {result['notes']}")
        if result.get("next_step"):
            lines.append(result["next_step"])
        return "\n".join(lines)

    async def route_active_flow(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Runs ahead of every other handler (registered in an earlier
        group): while a workout-log flow is open, every reply — including
        /cancel, /skip, /done, which would otherwise hit unrelated command
        handlers — is routed into the flow instead of its normal meaning.
        """
        if not self._authorized(update):
            return  # let the normal handler for this update issue the denial
        if not update.message or update.message.text is None:
            return
        flow = await asyncio.to_thread(self.workout_flows.active_flow)
        if flow is None:
            return
        outcome = await asyncio.to_thread(
            self.workout_flows.handle_reply, flow["id"], update.message.text
        )
        await update.message.reply_text(outcome["reply"])
        if outcome.get("finished") and outcome.get("result"):
            await update.message.reply_text(self._format_flow_result(outcome["result"]))
        raise ApplicationHandlerStop

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
        # Runs before every other handler so an open workout-log flow can
        # claim /cancel, /skip, /done and any free text before their normal
        # (unrelated) meaning would otherwise apply.
        app.add_handler(MessageHandler(filters.TEXT, self.route_active_flow), group=-1)
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("plan", self.plan))
        app.add_handler(CommandHandler("report", self.report))
        app.add_handler(CommandHandler("status", self.status))
        for cmd in ("done", "skipped", "felt"):
            app.add_handler(CommandHandler(cmd, self.feedback))
        app.add_handler(CommandHandler("meal", self.meal))
        app.add_handler(CommandHandler("water", self.water))
        app.add_handler(CommandHandler("reminders", self.reminders_list))
        app.add_handler(CommandHandler("edit_reminder", self.edit_reminder))
        for cmd in ("pause_reminder", "resume_reminder", "delete_reminder"):
            app.add_handler(CommandHandler(cmd, self.reminder_admin))
        app.add_handler(MessageHandler(filters.PHOTO, self.photo))
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
