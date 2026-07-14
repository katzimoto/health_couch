"""Seed pull_log for days that already hold Garmin-fed data.

Stops the gap healer from re-pulling ~90 days of history (and burning
Garmin's per-account rate-limit budget) on databases whose data predates the
pull_log table. See garmin_coach.migrations.mark_existing_days_pulled for the
full rationale and trade-off.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

from garmin_coach.storage.migrations import (
    mark_existing_days_pulled,
    unmark_assumed_pulled_days,
)

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    mark_existing_days_pulled(op.get_bind())


def downgrade() -> None:
    unmark_assumed_pulled_days(op.get_bind())
