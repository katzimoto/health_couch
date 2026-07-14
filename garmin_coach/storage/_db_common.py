"""Shared low-level helpers for the database layer.

Kept in their own module so repository mixins (e.g. the workout-merge mixin in
:mod:`garmin_coach._workout_merge_repo`) can import them without importing
:mod:`garmin_coach.database`, which would be a cycle. ``database`` re-imports
these names, so ``from garmin_coach.database import synthetic_activity_id``
keeps working.
"""

from __future__ import annotations

import itertools
import time as _time
from datetime import date, datetime


def _as_day(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


# Synthetic (negative) activity IDs for workouts not synced from Garmin.
# Time-based so they can never collide with Garmin's positive IDs across
# restarts; the counter disambiguates calls in the same millisecond.
_synthetic_seq = itertools.count()


def synthetic_activity_id() -> int:
    return -(int(_time.time() * 1000) * 1000 + next(_synthetic_seq) % 1000)
