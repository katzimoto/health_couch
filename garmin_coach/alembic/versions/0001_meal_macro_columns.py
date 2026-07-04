"""Add macro columns to the meal table.

Databases created before the Meal model gained macro fields fail every
log_meal INSERT that names them. Guarded (add-if-missing): production
databases where the startup column reconciler already added these columns
record this revision as a no-op.

Revision ID: 0001
Revises:
"""
from alembic import op

from garmin_coach.migrations import add_meal_macro_columns, drop_meal_macro_columns

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    add_meal_macro_columns(op.get_bind())


def downgrade() -> None:
    drop_meal_macro_columns(op.get_bind())
