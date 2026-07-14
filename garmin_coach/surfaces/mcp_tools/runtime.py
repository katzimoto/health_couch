"""Lazy, patchable runtime handles shared by the MCP tool modules.

The MCP tools used to close over module-level singletons built at import time
(``db = Database()`` ran on every import of ``mcp_server``, booting a DB
connection just to import the module). This module replaces that with lazy
accessors: nothing touches the database, Garmin, or Telegram until a tool
actually runs.

Design notes:

* ``get_db`` constructs the ``Database`` on first use and caches it. The
  ``Database`` class is imported *inside* the function so an
  ``importlib.reload`` of the database/config modules (as the migration tests
  do) is picked up — the cache is cleared via ``set_db(None)``.
* ``db``/``analyzer``/``reminders``/``workout_log_flows`` are thin forwarding
  proxies, so tool bodies keep reading ``db.foo()`` unchanged while the real
  object is resolved lazily on each attribute access. Point them all at a test
  database with a single ``set_db(database)``.
* ``send_telegram_message`` is re-exported here so tools call
  ``runtime.send_telegram_message(...)`` and tests can patch this one attribute.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from garmin_coach.surfaces.telegram_sender import send_telegram_message  # re-exported; patch here

# Don't re-hit ~9 Garmin endpoints because a chatty conversation asked twice;
# the hourly scheduler sync makes anything fresher than this rarely useful.
_SYNC_COOLDOWN_MIN = 10

_db: Any = None
# One authenticated Garmin client per process, created on first use so the
# server boots fine when Garmin auth is broken — only the sync tools then fail.
_garmin: Any = None


def get_db() -> Any:
    global _db
    if _db is None:
        from garmin_coach.storage.database import Database  # imported lazily so reloads are honoured
        _db = Database()
    return _db


def set_db(database: Any | None) -> None:
    """Point every runtime handle at ``database`` (or reset to lazy default with
    ``None``). The test seam; also clears the cached Garmin client."""
    global _db, _garmin
    _db = database
    _garmin = None


def get_analyzer() -> Any:
    from garmin_coach.domain.analysis import Analyzer
    return Analyzer(get_db())


def get_reminders() -> Any:
    from garmin_coach.domain.reminders import Reminders
    return Reminders(get_db())


def get_workout_log_flows() -> Any:
    from garmin_coach.domain.workout_flow import WorkoutLogFlows
    return WorkoutLogFlows(get_db())


class _LazyProxy:
    """Forwards attribute access to the object returned by ``factory()`` each
    call, so a swapped-in database (via ``set_db``) is always honoured."""

    __slots__ = ("_factory",)

    def __init__(self, factory: Callable[[], Any]) -> None:
        object.__setattr__(self, "_factory", factory)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._factory(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._factory(), name, value)


db = _LazyProxy(get_db)
analyzer = _LazyProxy(get_analyzer)
reminders = _LazyProxy(get_reminders)
workout_log_flows = _LazyProxy(get_workout_log_flows)


def _garmin_client() -> Any:
    global _garmin
    if _garmin is None:
        from garmin_coach.ingest.garmin_client import GarminClient
        _garmin = GarminClient(get_db())
    return _garmin


def set_garmin_client(client: Any) -> None:
    """Test seam: inject a fake Garmin client."""
    global _garmin
    _garmin = client


def _refresh_today_from_garmin() -> dict:
    """Sync callback handed to the coaching-context builder: pull today from
    Garmin now. Isolated so a broken Garmin auth degrades to stale data with a
    visible warning instead of failing the whole coaching call."""
    return _garmin_client().pull_day(date.today().isoformat())


def _minutes_since(ts: datetime | None) -> float | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # stored as UTC
    return round((datetime.now(timezone.utc) - ts).total_seconds() / 60, 1)
