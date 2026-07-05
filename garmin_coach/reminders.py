"""Telegram reminder engine: recurrence math, storage and dispatch bookkeeping.

:func:`compute_next_run` is the single source of truth for "when does this
reminder fire next" — a pure function over (time, timezone, recurrence, date)
so it's unit-testable without a database or a clock. :class:`Reminders` layers
CRUD on top of it: create (with exact-duplicate detection), partial edit,
pause/resume, soft delete, and the mark_sent/mark_failed/mark_missed
transitions the scheduler's dispatch loop drives.

Scheduling model: every reminder stores its next fire time in UTC
(``next_run_at``); the scheduler polls once a minute for rows that are due and
advances them after a successful send. Failures keep ``next_run_at`` in place
so the next poll retries, until :data:`Reminders.SEND_GRACE` has passed — then
the firing is recorded as *missed* and the schedule advances, so an outage
can't flood the chat with a backlog of stale reminders at recovery.

Soft delete (``deleted_at``) and a separate delivery table mean editing or
deleting a reminder never loses historical delivery records.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date as date_type, datetime, timedelta, timezone as tz_utc
from typing import Any
from zoneinfo import ZoneInfo

from sqlmodel import select

from .database import Database
from .models import TelegramReminder, TelegramReminderDelivery

log = logging.getLogger("garmin_coach.reminders")

DEFAULT_TIMEZONE = "Asia/Jerusalem"

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_MAX_ERROR_LEN = 500


def _utcnow() -> datetime:
    return datetime.now(tz_utc.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is stored as UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=tz_utc.utc) if dt.tzinfo is None else dt.astimezone(tz_utc.utc)


def compute_next_run(
    time_str: str,
    tz_name: str,
    recurrence: str,
    date_str: str | None = None,
    now: datetime | None = None,
) -> datetime | None:
    """Next fire time in UTC, or ``None`` when the reminder can never fire
    again (a "once" whose moment has passed, or an exhausted RRULE).

    Raises ``ValueError`` on malformed input (bad HH:MM, unknown timezone,
    unknown recurrence, "once" without a date, unparsable RRULE) so callers
    can reject a reminder at create/edit time rather than discover it in the
    dispatch loop.
    """
    match = _TIME_RE.match((time_str or "").strip())
    if not match:
        raise ValueError(f"time must be HH:MM (24h), got {time_str!r}")
    hour, minute = int(match[1]), int(match[2])
    try:
        tz = ZoneInfo((tz_name or DEFAULT_TIMEZONE).strip())
    except Exception as exc:  # noqa: BLE001 — ZoneInfoNotFoundError, ValueError…
        raise ValueError(f"unknown timezone {tz_name!r}") from exc

    now_utc = as_utc(now) or _utcnow()
    local_now = now_utc.astimezone(tz)

    def at(d: date_type) -> datetime:
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)

    def parse_date(value: str) -> date_type:
        try:
            return date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"date must be YYYY-MM-DD, got {value!r}") from exc

    rule = (recurrence or "daily").strip()
    if rule.upper().startswith("RRULE"):
        from dateutil.rrule import rrulestr

        anchor = parse_date(date_str) if date_str else local_now.date()
        try:
            parsed = rrulestr(rule, dtstart=at(anchor))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"invalid RRULE {rule!r}: {exc}") from exc
        nxt = parsed.after(local_now)
        return nxt.astimezone(tz_utc.utc) if nxt else None

    rule = rule.lower()
    if rule == "once":
        if not date_str:
            raise ValueError("recurrence 'once' requires a date (YYYY-MM-DD)")
        candidate = at(parse_date(date_str))
        return candidate.astimezone(tz_utc.utc) if candidate > local_now else None
    if rule == "daily":
        candidate = at(local_now.date())
        if candidate <= local_now:
            candidate = at(local_now.date() + timedelta(days=1))
        return candidate.astimezone(tz_utc.utc)
    if rule in ("weekdays", "weekly"):
        anchor_weekday = (
            parse_date(date_str).weekday()
            if rule == "weekly" and date_str
            else local_now.weekday()
        )
        day = local_now.date()
        for _ in range(9):  # at most a week + 2 days to scan
            candidate = at(day)
            matches = (
                day.weekday() < 5 if rule == "weekdays"
                else day.weekday() == anchor_weekday
            )
            if matches and candidate > local_now:
                return candidate.astimezone(tz_utc.utc)
            day += timedelta(days=1)
    raise ValueError(
        f"unknown recurrence {recurrence!r} "
        f"(use once | daily | weekly | weekdays | an RRULE string)"
    )


# The four recommended Health Coach reminders (installed on demand via the
# create_default_health_reminders MCP tool; create() dedupes re-installs).
PRESET_REMINDERS: tuple[dict[str, Any], ...] = (
    {
        "title": "Morning plan",
        "time": "08:00",
        "tags": ["health", "plan"],
        "message": (
            "Good morning. Generate today’s Health Coach plan: Garmin "
            "readiness, training/recovery, nutrition targets, hydration, "
            "and one behavior focus."
        ),
    },
    {
        "title": "Lunch log",
        "time": "13:00",
        "tags": ["health", "meal", "lunch"],
        "message": "Log lunch with the coach. Send photo/description or say skipped.",
    },
    {
        "title": "Dinner log",
        "time": "20:00",
        "tags": ["health", "meal", "dinner"],
        "message": "Log dinner with the coach. Send photo/description or say skipped.",
    },
    {
        "title": "Evening report",
        "time": "21:30",
        "tags": ["health", "report"],
        "message": (
            "Generate today’s Health Coach report: plan vs actual, "
            "meals/macros, hydration, steps, training, recovery, and what "
            "to improve tomorrow."
        ),
    },
)


class Reminders:
    """CRUD + dispatch bookkeeping over the telegram_reminder tables."""

    # A due reminder unsent for longer than this is recorded as missed and
    # skipped to its next occurrence instead of firing hours late.
    SEND_GRACE = timedelta(minutes=15)

    def __init__(self, db: Database) -> None:
        self.db = db

    # ── Row → dict ─────────────────────────────────────────────────────────────

    @staticmethod
    def _loads(raw: str | None) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None  # a corrupt JSON column must not break reads

    @classmethod
    def _dump(cls, row: TelegramReminder) -> dict[str, Any]:
        out = row.model_dump()
        out["tags"] = cls._loads(out.pop("tags_json", None))
        out["metadata"] = cls._loads(out.pop("metadata_json", None))
        return out

    # ── CRUD ───────────────────────────────────────────────────────────────────

    def create(
        self,
        title: str,
        message: str,
        time: str,
        timezone: str = DEFAULT_TIMEZONE,
        recurrence: str = "daily",
        date: str | None = None,
        enabled: bool = True,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store a reminder and precompute its next fire time.

        Exact duplicates (same title, message, time and recurrence among
        non-deleted reminders) return the existing row with
        ``deduplicated: true`` instead of creating a second one, so preset
        installs and repeated ChatGPT calls are idempotent.
        """
        if not (title or "").strip():
            raise ValueError("title must not be empty")
        if not (message or "").strip():
            raise ValueError("message must not be empty")
        next_run = compute_next_run(time, timezone, recurrence, date)  # validates
        with self.db.session() as s:
            duplicate = s.exec(
                select(TelegramReminder).where(
                    TelegramReminder.deleted_at == None,  # noqa: E711
                    TelegramReminder.title == title,
                    TelegramReminder.message == message,
                    TelegramReminder.time == time,
                    TelegramReminder.recurrence == recurrence,
                )
            ).first()
            if duplicate is not None:
                out = self._dump(duplicate)
                out["deduplicated"] = True
                return out
            row = TelegramReminder(
                title=title,
                message=message,
                time=time,
                timezone=timezone,
                recurrence=recurrence,
                date=date,
                enabled=enabled,
                tags_json=json.dumps(tags, ensure_ascii=False) if tags else None,
                metadata_json=(
                    json.dumps(metadata, ensure_ascii=False) if metadata else None
                ),
                next_run_at=next_run,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            out = self._dump(row)
            out["deduplicated"] = False
            return out

    def get(self, reminder_id: int) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.get(TelegramReminder, reminder_id)
        return self._dump(row) if row is not None else None

    def list(
        self,
        enabled_only: bool = False,
        tag: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        with self.db.session() as s:
            stmt = select(TelegramReminder).order_by(
                TelegramReminder.time, TelegramReminder.id
            )
            if not include_deleted:
                stmt = stmt.where(TelegramReminder.deleted_at == None)  # noqa: E711
            if enabled_only:
                stmt = stmt.where(TelegramReminder.enabled == True)  # noqa: E712
            rows = s.exec(stmt).all()
        out = [self._dump(r) for r in rows]
        if tag:
            out = [r for r in out if tag in (r["tags"] or [])]
        return out

    def edit(self, reminder_id: int, **fields: Any) -> dict[str, Any] | None:
        """Partial update: only non-``None`` fields change (so tags/date can't
        be *cleared* here — recreate the reminder for that). ``next_run_at``
        is recomputed from the merged state; ``created_at`` is untouched and
        delivery history is unaffected. Returns ``None`` for unknown or
        deleted reminders — editing never creates a new row.
        """
        editable = {
            "title", "message", "time", "timezone", "recurrence", "date",
            "enabled", "tags", "metadata",
        }
        unknown = set(fields) - editable
        if unknown:
            raise ValueError(f"unknown reminder fields: {sorted(unknown)}")
        with self.db.session() as s:
            row = s.get(TelegramReminder, reminder_id)
            if row is None or row.deleted_at is not None:
                return None
            for key, value in fields.items():
                if value is None:
                    continue
                if key == "tags":
                    row.tags_json = json.dumps(value, ensure_ascii=False)
                elif key == "metadata":
                    row.metadata_json = json.dumps(value, ensure_ascii=False)
                else:
                    setattr(row, key, value)
            # Validate + recompute BEFORE commit so a bad edit changes nothing.
            row.next_run_at = compute_next_run(
                row.time, row.timezone, row.recurrence, row.date
            )
            row.updated_at = _utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._dump(row)

    def set_enabled(self, reminder_id: int, enabled: bool) -> dict[str, Any] | None:
        """Pause (False) or resume (True). Resume recomputes ``next_run_at``
        so a reminder paused for a week doesn't fire immediately as overdue."""
        with self.db.session() as s:
            row = s.get(TelegramReminder, reminder_id)
            if row is None or row.deleted_at is not None:
                return None
            row.enabled = enabled
            if enabled:
                row.next_run_at = compute_next_run(
                    row.time, row.timezone, row.recurrence, row.date
                )
            row.updated_at = _utcnow()
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._dump(row)

    def delete(self, reminder_id: int) -> bool:
        """Soft delete: stops all future sends, keeps the row and its delivery
        history for later analysis."""
        with self.db.session() as s:
            row = s.get(TelegramReminder, reminder_id)
            if row is None or row.deleted_at is not None:
                return False
            row.deleted_at = _utcnow()
            row.enabled = False
            row.next_run_at = None
            row.updated_at = row.deleted_at
            s.add(row)
            s.commit()
        return True

    # ── Dispatch support (driven by the scheduler's poll loop) ─────────────────

    def due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Enabled, non-deleted reminders whose ``next_run_at`` has arrived."""
        now = as_utc(now) or _utcnow()
        with self.db.session() as s:
            rows = s.exec(
                select(TelegramReminder).where(
                    TelegramReminder.deleted_at == None,  # noqa: E711
                    TelegramReminder.enabled == True,  # noqa: E712
                    TelegramReminder.next_run_at != None,  # noqa: E711
                    TelegramReminder.next_run_at <= now,
                ).order_by(TelegramReminder.next_run_at)
            ).all()
        return [self._dump(r) for r in rows]

    def _advance(self, row: TelegramReminder, now: datetime | None) -> None:
        """Move a reminder past the firing that was just handled."""
        if row.recurrence.strip().lower() == "once":
            row.next_run_at = None
            row.enabled = False
            return
        try:
            row.next_run_at = compute_next_run(
                row.time, row.timezone, row.recurrence, row.date, now=now
            )
        except ValueError:
            # A stored-corrupt schedule must not wedge the dispatch loop in a
            # fire-forever state; park the reminder instead.
            log.exception("Reminder %s has an invalid schedule — disabling.", row.id)
            row.next_run_at = None
            row.enabled = False
        if row.next_run_at is None and row.enabled:
            row.enabled = False  # exhausted RRULE — nothing left to fire

    def _record(
        self,
        reminder_id: int | None,
        status: str,
        telegram_message_id: int | None = None,
        error: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        with self.db.session() as s:
            s.add(
                TelegramReminderDelivery(
                    reminder_id=reminder_id,
                    status=status,
                    telegram_message_id=telegram_message_id,
                    error=error[:_MAX_ERROR_LEN] if error else None,
                    meta_json=(
                        json.dumps(meta, ensure_ascii=False) if meta else None
                    ),
                )
            )
            s.commit()

    def mark_sent(
        self, reminder_id: int, telegram_message_id: int | None, now: datetime | None = None
    ) -> None:
        """Record a successful delivery and advance the schedule."""
        with self.db.session() as s:
            row = s.get(TelegramReminder, reminder_id)
            if row is None:
                return
            row.last_sent_at = as_utc(now) or _utcnow()
            self._advance(row, now)
            s.add(row)
            s.commit()
        self._record(reminder_id, "sent", telegram_message_id=telegram_message_id)

    def mark_failed(self, reminder_id: int, error: str) -> None:
        """Record a failed attempt WITHOUT advancing — the next poll retries,
        until SEND_GRACE turns the firing into a miss."""
        self._record(reminder_id, "error", error=error)

    def mark_missed(self, reminder_id: int, now: datetime | None = None) -> None:
        """Record a firing that was never delivered (service down / persistent
        send failures past the grace window) and advance the schedule."""
        with self.db.session() as s:
            row = s.get(TelegramReminder, reminder_id)
            if row is None:
                return
            self._advance(row, now)
            s.add(row)
            s.commit()
        self._record(reminder_id, "missed", error="missed delivery window")

    def record_ad_hoc(
        self,
        status: str,
        telegram_message_id: int | None = None,
        error: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Delivery record for an immediate (non-scheduled) send."""
        meta: dict[str, Any] = {}
        if tags:
            meta["tags"] = tags
        if metadata:
            meta["metadata"] = metadata
        self._record(
            None, status, telegram_message_id=telegram_message_id,
            error=error, meta=meta or None,
        )

    def deliveries(
        self, reminder_id: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Delivery history, newest first. Omit ``reminder_id`` for all
        (including ad-hoc sends)."""
        with self.db.session() as s:
            stmt = select(TelegramReminderDelivery).order_by(
                TelegramReminderDelivery.id.desc()
            ).limit(limit)
            if reminder_id is not None:
                stmt = stmt.where(
                    TelegramReminderDelivery.reminder_id == reminder_id
                )
            rows = s.exec(stmt).all()
        out = []
        for r in rows:
            d = r.model_dump()
            d["meta"] = self._loads(d.pop("meta_json", None))
            out.append(d)
        return out

    # ── Presets ────────────────────────────────────────────────────────────────

    def create_presets(self, timezone: str = DEFAULT_TIMEZONE) -> list[dict[str, Any]]:
        """Install the recommended Health Coach reminders (morning plan, lunch
        and dinner logs, evening report). Idempotent — re-running dedupes."""
        return [
            self.create(
                title=preset["title"],
                message=preset["message"],
                time=preset["time"],
                timezone=timezone,
                recurrence="daily",
                tags=preset["tags"],
            )
            for preset in PRESET_REMINDERS
        ]
