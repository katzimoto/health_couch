"""Garmin-sync throttling and data-freshness classification.

Pure functions, separated from I/O so they are unit-testable and shared by the
MCP ``sync_garmin`` tool and the coach's report-context builder.

The one invariant this module encodes: **only Garmin *network* sync is rate
limited.** Reading Health Coach data from the local database is never throttled
here — callers must not consult :func:`evaluate_sync` before a local read.
"""

from __future__ import annotations

from datetime import datetime, timezone

# A Garmin pull younger than this is "fresh": the scheduler syncs hourly, so
# ~90 minutes covers a normal cadence plus slack for one delayed run.
FRESH_MAX_MINUTES = 90
# Older than fresh but still recent enough to be usable cached data (same day).
CACHED_MAX_MINUTES = 24 * 60


def minutes_since(ts: datetime | None, now: datetime | None = None) -> float | None:
    """Whole-ish minutes between ``ts`` (stored UTC, possibly naive) and now."""
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)  # pull timestamps are stored as UTC
    return round((now - ts).total_seconds() / 60, 1)


def classify_freshness(minutes_since_last_sync: float | None) -> str:
    """Map minutes-since-sync to a freshness label the report can state.

    ``none`` (never synced) → ``fresh`` → ``cached`` → ``stale``. Anything but
    ``fresh`` is still usable — the caller keeps going and labels it cached.
    """
    if minutes_since_last_sync is None:
        return "none"
    if minutes_since_last_sync <= FRESH_MAX_MINUTES:
        return "fresh"
    if minutes_since_last_sync <= CACHED_MAX_MINUTES:
        return "cached"
    return "stale"


def evaluate_sync(
    minutes_since_last_sync: float | None,
    *,
    force: bool,
    default_min_interval: float,
    force_min_interval: float,
) -> tuple[bool, str]:
    """Decide whether a Garmin *network* sync may run now.

    Returns ``(allowed, reason)``. A normal refresh must wait
    ``default_min_interval`` minutes since the last pull; an explicit ``force``
    lowers (but does not remove) the floor to ``force_min_interval``. A sync is
    always allowed when nothing has ever been pulled.

    This governs only Garmin network sync. Local Health Coach reads must never
    call this.
    """
    threshold = force_min_interval if force else default_min_interval
    if minutes_since_last_sync is None:
        return True, "never_synced"
    if minutes_since_last_sync < threshold:
        return False, "throttled_recent_sync"
    return True, ("forced" if force else "stale")
