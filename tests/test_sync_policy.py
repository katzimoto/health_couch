"""Tests for Garmin-sync throttling and data-freshness classification, and the
coach's always-read report-context helper.

Covers the acceptance scenarios: fresh sync, stale-but-allowed sync, stale-but-
throttled sync, sync failure with cached data available, and no cached data.
The invariant under test: only Garmin *network* sync is rate limited — reading
Health Coach data is always allowed and never falls back to a generic plan
while any local data exists.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from garmin_coach.coach import Coach, _has_usable_data
from garmin_coach.database import Database
from garmin_coach.sync_policy import (
    classify_freshness,
    evaluate_sync,
    minutes_since,
)


# ── Pure freshness classification ───────────────────────────────────────────────

def test_classify_freshness_buckets() -> None:
    assert classify_freshness(None) == "none"
    assert classify_freshness(0) == "fresh"
    assert classify_freshness(90) == "fresh"
    assert classify_freshness(91) == "cached"
    assert classify_freshness(24 * 60) == "cached"
    assert classify_freshness(24 * 60 + 1) == "stale"


def test_minutes_since_handles_naive_and_none() -> None:
    assert minutes_since(None) is None
    now = datetime.now(timezone.utc)
    # A naive timestamp is treated as UTC.
    naive = (now - timedelta(minutes=30)).replace(tzinfo=None)
    assert minutes_since(naive, now=now) == pytest.approx(30.0, abs=0.1)


# ── Sync throttling decision ────────────────────────────────────────────────────

def test_sync_allowed_when_never_synced() -> None:
    allowed, reason = evaluate_sync(
        None, force=False, default_min_interval=60, force_min_interval=10
    )
    assert allowed is True
    assert reason == "never_synced"


def test_fresh_sync_is_throttled() -> None:
    # last pull 12 minutes ago, well inside the 60-min default interval.
    allowed, reason = evaluate_sync(
        12, force=False, default_min_interval=60, force_min_interval=10
    )
    assert allowed is False
    assert reason == "throttled_recent_sync"


def test_stale_sync_is_allowed() -> None:
    allowed, reason = evaluate_sync(
        75, force=False, default_min_interval=60, force_min_interval=10
    )
    assert allowed is True
    assert reason == "stale"


def test_force_lowers_but_does_not_remove_the_floor() -> None:
    # 30 min ago: throttled by default, allowed with force (>10-min floor).
    assert evaluate_sync(30, force=False, default_min_interval=60, force_min_interval=10)[0] is False
    assert evaluate_sync(30, force=True, default_min_interval=60, force_min_interval=10) == (
        True,
        "forced",
    )
    # 5 min ago: even force is throttled (inside its 10-min floor).
    assert evaluate_sync(5, force=True, default_min_interval=60, force_min_interval=10) == (
        False,
        "throttled_recent_sync",
    )


# ── Coach report context always reads local data ────────────────────────────────

@pytest.fixture()
def coach(tmp_path) -> Coach:
    return Coach(Database(path=str(tmp_path / "ctx.db")))


def _record_pull(db: Database, minutes_ago: float) -> None:
    """Record a Garmin pull whose timestamp is ``minutes_ago`` in the past."""
    db.record_pull("2026-07-06", {"sleep": "ok"})
    # record_pull stamps ts=now; rewrite it to the desired age.
    from garmin_coach.models import PullLog
    from sqlmodel import select

    with db.session() as s:
        row = s.exec(select(PullLog).order_by(PullLog.ts.desc()).limit(1)).first()
        row.ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
        s.add(row)
        s.commit()


def test_context_reports_fresh_when_recently_synced(coach: Coach) -> None:
    _record_pull(coach.db, minutes_ago=10)
    ctx = coach.get_health_context_for_report(mode="morning")
    assert ctx["data_freshness"]["status"] == "fresh"
    assert ctx["data_freshness"]["using_cached_data"] is False


def test_context_reports_cached_when_stale_and_still_usable(coach: Coach) -> None:
    # A stale Garmin pull plus a logged meal → cached, but usable data exists.
    _record_pull(coach.db, minutes_ago=300)
    coach.db.add_meal(name="Lunch", calories=600, protein_g=40)
    ctx = coach.get_health_context_for_report(mode="morning")
    assert ctx["data_freshness"]["status"] == "cached"
    assert ctx["data_freshness"]["using_cached_data"] is True
    assert ctx["has_usable_data"] is True


def test_context_has_usable_data_from_profile_even_without_garmin(coach: Coach) -> None:
    # No Garmin pull at all, but a profile exists → still personalisable.
    coach.db.set_profile(goal_type="fat_loss", calorie_target=2000)
    ctx = coach.get_health_context_for_report(mode="morning")
    assert ctx["data_freshness"]["status"] == "none"
    assert ctx["has_usable_data"] is True


def test_context_has_no_usable_data_on_empty_db(coach: Coach) -> None:
    ctx = coach.get_health_context_for_report(mode="evening")
    assert ctx["data_freshness"]["status"] == "none"
    assert ctx["has_usable_data"] is False


def test_has_usable_data_helper_detects_each_source() -> None:
    assert _has_usable_data({"profile": {"id": 1}}) is True
    assert _has_usable_data({"analysis": {"available": True}}) is True
    assert _has_usable_data({"training_plans_today": [{"id": 1}]}) is True
    assert _has_usable_data({"health_events_today": [{"kind": "meal"}]}) is True
    assert _has_usable_data({"hydration_today": [{"intake_ml": 500}]}) is True
    assert _has_usable_data({"nutrition_today": [{"meal_count": 2}]}) is True
    assert _has_usable_data({"nutrition_today": [{"meal_count": 0}]}) is False
    assert _has_usable_data({"analysis": {"available": False}}) is False
    assert _has_usable_data({}) is False
