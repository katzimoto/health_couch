"""Field-level workout merge schema.

Adds the ``field_sources`` provenance column to ``workout`` and the
``workout_source_link`` table that links a manual strength log and a Garmin
activity of the same session into one canonical workout (manual exercise
details + Garmin physiology/load), keeping both source rows. See
garmin_coach.migrations.add_workout_source_link_schema for the guarded,
idempotent DDL.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

from garmin_coach.storage.migrations import (
    add_workout_source_link_schema,
    drop_workout_source_link_schema,
)

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_workout_source_link_schema(op.get_bind())


def downgrade() -> None:
    drop_workout_source_link_schema(op.get_bind())
