"""On-demand Garmin sync and sync-status MCP tools.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

import logging
from datetime import date

from .runtime import _SYNC_COOLDOWN_MIN, _garmin_client, _minutes_since, db

log = logging.getLogger("garmin_coach.mcp")

__all__ = ["sync_garmin", "get_sync_status"]


def sync_garmin(day: str | None = None, force: bool = False) -> dict:
    """Pull fresh data from Garmin Connect right now instead of waiting for
    the hourly sync — use before answering when get_sync_status shows stale
    data. Defaults to today; pass ``day`` (YYYY-MM-DD) to refresh a specific
    date. Skipped if today was already synced within the last few minutes
    unless ``force=true``. Returns per-metric results."""
    last = db.last_pull()
    if day is None and not force and last is not None:
        minutes_ago = _minutes_since(last["ts"])
        if minutes_ago is not None and minutes_ago < _SYNC_COOLDOWN_MIN:
            return {
                "synced": False,
                "reason": f"already synced {minutes_ago:g} minutes ago — "
                          "data is current (use force=true to override)",
                "last_synced_day": last["day"],
                "minutes_since_last_sync": minutes_ago,
            }
    target_day = day or date.today().isoformat()
    try:
        results = _garmin_client().pull_day(target_day)
    except Exception as exc:  # noqa: BLE001 — surface auth/network problems to the caller
        log.exception("On-demand Garmin sync failed")
        return {
            "synced": False,
            "error": f"Garmin sync failed: {exc}",
            "hint": "If this persists, the cached Garmin tokens may have "
                    "expired — run scripts/garmin_login.py on the server.",
        }
    ok = sum(1 for status in results.values() if status == "ok")
    return {
        "synced": ok > 0,
        "day": target_day,
        "metrics_ok": ok,
        "metrics_failed": len(results) - ok,
        "results": results,
    }
def get_sync_status() -> dict:
    """When Garmin data was last synced: the day covered, how many minutes
    ago the sync ran, its per-metric results, and whether it's stale (the
    scheduler syncs hourly, so >90 minutes means something is wrong). Check
    this before trusting today's numbers; use sync_garmin to refresh."""
    last = db.last_pull()
    if last is None:
        return {
            "synced_ever": False,
            "detail": "No Garmin pull recorded yet — run a backfill or sync_garmin.",
        }
    minutes_ago = _minutes_since(last["ts"])
    return {
        "synced_ever": True,
        "last_synced_day": last["day"],
        "last_synced_at": last["ts"].isoformat() if last["ts"] else None,
        "minutes_since_last_sync": minutes_ago,
        "stale": minutes_ago is None or minutes_ago > 90,
        "last_results": last["status"],
    }


TOOLS = [sync_garmin, get_sync_status]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
