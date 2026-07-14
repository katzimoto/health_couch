"""Backfill workout provenance and estimate missing training loads.

Infers source for pre-existing workouts (positive activity IDs = garmin,
negative = manual/imported), stamps load_source, and fills NULL training_load
with the documented heuristic estimate so get_training_load stops reporting
zero for weeks that contained real walking/running/lifting.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

from garmin_coach.storage.migrations import (
    backfill_workout_source_and_load,
    clear_estimated_workout_loads,
)

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    backfill_workout_source_and_load(op.get_bind())


def downgrade() -> None:
    clear_estimated_workout_loads(op.get_bind())
