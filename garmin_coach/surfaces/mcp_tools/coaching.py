"""The flagship daily-coaching MCP tools and plan reads.

Thin wrappers over the shared runtime handles in
:mod:`garmin_coach.mcp_tools.runtime`; registered on the server by
``register(mcp)``. Split out of the former monolithic ``mcp_server``.
"""

from __future__ import annotations

from garmin_coach.domain.coaching_context import build_coaching_context
from .runtime import _refresh_today_from_garmin, db

__all__ = ["get_today_coaching_context", "generate_daily_plan", "get_feedback", "get_latest_plan"]


def get_today_coaching_context(
    day: str | None = None,
    refresh_if_stale: bool = True,
    include_recommendation: bool = True,
) -> dict:
    """THE daily-coaching call: everything needed to answer "based on all my
    current data, what should I do today?" in one structured payload.

    Returns data_freshness (which sources were retrieved vs missing, and whether
    a refresh ran), profile, recovery (classification + confidence + reasons),
    sleep (against the user's *configured* target, not a hard-coded 8h),
    activity, training_load, recent_workouts, strength_history, body_composition,
    nutrition, hydration (with configured targets; missing intake stays unknown,
    never zero), pending_training_plan, flags, data_quality_warnings and — when
    ``include_recommendation`` — a structured recommendation (training decision,
    intensity, suggested session, step/cardio/sleep/hydration targets, nutrition
    priorities, and one top priority).

    With ``refresh_if_stale`` (default true) a stale Garmin sync (>90 min) is
    refreshed first; if Garmin is unreachable the latest cached data is used and
    the failure is reported under data_freshness.refresh rather than returning a
    generic "connector unavailable"."""
    return build_coaching_context(
        db,
        day=day,
        refresh_if_stale=refresh_if_stale,
        include_recommendation=include_recommendation,
        garmin_sync=_refresh_today_from_garmin if refresh_if_stale else None,
    )
def generate_daily_plan(
    day: str | None = None,
    refresh_if_stale: bool = True,
) -> dict:
    """Backward-compatible alias / thin wrapper over get_today_coaching_context
    that always includes the structured recommendation — the shape scheduled
    daily jobs consume to build and deliver the morning plan."""
    return build_coaching_context(
        db,
        day=day,
        refresh_if_stale=refresh_if_stale,
        include_recommendation=True,
        garmin_sync=_refresh_today_from_garmin if refresh_if_stale else None,
    )
def get_feedback(days: int = 30) -> list[dict]:
    """Feedback notes logged via the Telegram coach (/done, /skipped, /felt)
    over the last ``days``, oldest first."""
    return db.recent_feedback(days=max(1, min(days, 365)))
def get_latest_plan() -> dict | None:
    """The most recently generated morning plan (day + full plan text, plus
    structured details when available), or null if none exists yet."""
    return db.last_plan()


TOOLS = [get_today_coaching_context, generate_daily_plan, get_feedback, get_latest_plan]

def register(mcp) -> None:
    """Register this module's tools on the FastMCP server."""
    for _tool in TOOLS:
        mcp.tool(_tool)
