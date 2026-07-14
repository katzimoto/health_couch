"""JournalMixin: Health events, conversation memory, feedback, saved plans, and the
Garmin pull log.

Extracted from :class:`garmin_coach.database.Database` as a mixin; the
composed ``Database`` provides shared primitives (``session``, ``_upsert``,
``_cutoff``, ``_view_rows``). Not instantiated on its own.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from sqlmodel import select

from garmin_coach.storage._db_common import _as_day
from garmin_coach.storage.models import (
    Conversation,
    Feedback,
    HealthEvent,
    Plan,
    PlanDetail,
    PullLog,
)

class JournalMixin:
    """Health events, conversation memory, feedback, saved plans, and the"""

    # ── Health events (structured Telegram-captured logs) ─────────────────────

    @staticmethod
    def _event_dict(row: HealthEvent) -> dict[str, Any]:
        out = row.model_dump()
        raw = out.pop("payload_json", None)
        try:
            out["payload"] = json.loads(raw) if raw else None
        except ValueError:
            out["payload"] = raw  # a corrupt payload must not break event reads
        return out

    def add_health_event(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        day: str | date | None = None,
        source: str = "telegram",
    ) -> dict[str, Any]:
        with self.session() as s:
            row = HealthEvent(
                kind=kind,
                source=source,
                day=_as_day(day or date.today()),
                payload_json=(
                    json.dumps(payload, ensure_ascii=False)
                    if payload is not None else None
                ),
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            return self._event_dict(row)

    def recent_health_events(
        self, days: int = 7, kind: str | None = None
    ) -> list[dict[str, Any]]:
        with self.session() as s:
            stmt = (
                select(HealthEvent)
                .where(HealthEvent.day >= self._cutoff(days))
                .order_by(HealthEvent.day, HealthEvent.id)
            )
            if kind:
                stmt = stmt.where(HealthEvent.kind == kind)
            rows = s.exec(stmt).all()
        return [self._event_dict(r) for r in rows]

    def health_events_for_day(self, day: str | date) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(HealthEvent)
                .where(HealthEvent.day == _as_day(day))
                .order_by(HealthEvent.id)
            ).all()
        return [self._event_dict(r) for r in rows]

    # ── Conversation memory ────────────────────────────────────────────────────

    def add_message(self, role: str, content: str) -> None:
        with self.session() as s:
            s.add(Conversation(role=role, content=content))
            s.commit()

    def recent_messages(self, limit: int = 20) -> list[dict[str, str]]:
        """Return the last ``limit`` messages in chronological order."""
        with self.session() as s:
            rows = s.exec(
                select(Conversation).order_by(Conversation.id.desc()).limit(limit)
            ).all()
        return [{"role": r.role, "content": r.content} for r in reversed(rows)]

    def add_feedback(self, note: str, day: str | date | None = None) -> None:
        with self.session() as s:
            s.add(Feedback(day=_as_day(day or date.today()), note=note))
            s.commit()

    def recent_feedback(self, days: int = 7) -> list[dict[str, Any]]:
        with self.session() as s:
            rows = s.exec(
                select(Feedback).order_by(Feedback.id.desc()).limit(days * 4)
            ).all()
        return [{"day": r.day, "note": r.note} for r in reversed(rows)]

    def save_plan(
        self, day: str | date, plan: str, details: dict[str, Any] | None = None
    ) -> None:
        with self.session() as s:
            s.merge(Plan(day=_as_day(day), ts=datetime.now(timezone.utc), plan=plan))
            if details is not None:
                s.merge(
                    PlanDetail(
                        day=_as_day(day),
                        ts=datetime.now(timezone.utc),
                        data=json.dumps(details, ensure_ascii=False),
                    )
                )
            s.commit()

    def last_plan(self) -> dict[str, Any] | None:
        with self.session() as s:
            row = s.exec(select(Plan).order_by(Plan.day.desc()).limit(1)).first()
            if row is None:
                return None
            out: dict[str, Any] = {"day": row.day, "plan": row.plan}
            detail = s.get(PlanDetail, row.day)
        if detail is not None:
            try:
                out["details"] = json.loads(detail.data)
            except ValueError:
                pass  # a corrupt details row must not break plan reads
        return out

    # ── Pull log (which days Garmin has been pulled for) ───────────────────────

    def record_pull(self, day: str | date, status: dict[str, str]) -> None:
        self._upsert(PullLog(day=_as_day(day), status=json.dumps(status)))

    def pulled_days(self, start: str | date, end: str | date) -> set[str]:
        """Days in ``[start, end]`` that have a successful pull recorded."""
        with self.session() as s:
            rows = s.exec(
                select(PullLog.day)
                .where(PullLog.day >= _as_day(start), PullLog.day <= _as_day(end))
            ).all()
        return set(rows)

    def last_pull(self) -> dict[str, Any] | None:
        """The most recent Garmin pull: the day it covered, when it ran, and
        its per-metric results. ``ts`` is updated on every re-pull of a day,
        so this reflects actual sync recency, not just the newest day."""
        with self.session() as s:
            row = s.exec(select(PullLog).order_by(PullLog.ts.desc()).limit(1)).first()
        if row is None:
            return None
        try:
            status = json.loads(row.status) if row.status else None
        except ValueError:
            status = row.status
        return {"day": row.day, "ts": row.ts, "status": status}
