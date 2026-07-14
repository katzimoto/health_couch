# Daily Coaching Upgrade

This document describes the Priority-0 upgrade that makes the Health Coach
server a reliable, data-driven daily coach: a single unified coaching endpoint,
a configurable sleep target, persistent hydration configuration, and nutrition
provenance/import fixes. It also records how existing production data is handled.

## 1. Unified daily coaching context

**Tool:** `get_today_coaching_context(day=None, refresh_if_stale=True, include_recommendation=True)`
**Alias:** `generate_daily_plan(day=None, refresh_if_stale=True)` (always includes the recommendation).

One call returns everything needed to answer *"based on all my current data,
what should I do today?"*. The logic lives in `garmin_coach/coaching_context.py`
and is deliberately split so the coaching decisions are unit-testable without a
DB, clock, or network:

- `classify_recovery(...)` — pure. Weighs sleep **against the user's configured
  target** (not a hard-coded 8h), HRV status/trend, resting-HR vs baseline,
  stress, Body Battery and acute:chronic load into a transparent
  `{status, confidence, reasons}`. A short night alone *reduces* (moderate); it
  never cancels training by itself. Reported pain/illness escalates to
  `compromised`. Statuses: `good | moderate | low | compromised`.
- `build_recommendation(...)` — pure. Maps the recovery status to a structured
  recommendation: `training_decision` (`normal_strength | reduced_strength |
  active_recovery | rest`), `training_intensity`, `suggested_session`,
  `step_target`, `cardio_target`, `sleep_target_hours`, `hydration_target_ml`,
  `nutrition_priorities`, and one `top_priority`.
- `build_coaching_context(db, ...)` — assembles the full payload (data
  freshness, profile, recovery, sleep, activity, training load, recent workouts,
  strength history, body composition, nutrition, hydration, pending training
  plan, flags, data-quality warnings, recommendation).

### Freshness, refresh, and scheduled-job access

`data_freshness.sources` reports, per source, whether data was actually
retrieved — so a caller sees *what was missing* instead of a blanket "connector
unavailable". When `refresh_if_stale` is set and the last Garmin pull is stale
(>90 min), an injected `garmin_sync` callback runs first; if Garmin is
unreachable the latest cached data is still returned and the failure is recorded
under `data_freshness.refresh` (never a crash).

The context builder is a plain function, so **scheduled jobs call the same read
path** an interactive conversation uses (`build_coaching_context(db)`), with no
connector/OAuth round-trip. The 07:30 morning-plan job now grounds on it: the
coach's data block includes the structured recovery classification and
recommendation (`coach.py::_data_context`, `refresh_if_stale=False` so the plan
never blocks on a network pull — the scheduler already syncs hourly).

## 2. Configurable sleep need

The hard-coded 8-hour assumption is gone. The baseline is **7.0h**.

New `Profile` fields (all nullable): `sleep_target_hours`,
`sleep_preferred_min_hours`, `sleep_preferred_max_hours`,
`sleep_minimum_recovery_hours`, `sleep_target_effective_from`.

New table `sleep_target_history` stores **effective-dated** target changes so
historical sleep-debt numbers stay reproducible after a change. Resolution
(`Database.sleep_target_for(day)`): latest history row with
`effective_from <= day` → profile's current value → 7.0h default.

- **Tool:** `set_sleep_target(target_hours, effective_from=None, minimum_recovery_hours=None, preferred_min_hours=None, preferred_max_hours=None, note=None)`.
- **Sleep debt** (`Analyzer.sleep_debt`) is now the shortfall vs the *configured*
  target, resolved **per night** through the effective-dated history, clamped
  at `max(0, target - actual)` per the coaching model (a long night doesn't pay
  back a short one). It is surfaced as an **estimate**, not a physiological
  measurement (`report["sleep_debt_7d"]`, `report["sleep_target_hours"]`).

Training is never gated on sleep alone — see `classify_recovery`.

## 3. Persistent hydration configuration

New nullable `Profile` fields: `hydration_baseline_target_ml` (default 2750),
`hydration_training_day_target_ml` (default 3250), `hydration_hot_day_target_ml`
(default 3250), `hydration_medical_limit_note`. Read via
`Database.hydration_targets()` and settable through `set_profile`.

**Missing hydration stays unknown — never zero.** The context reports
`hydration.today = None` and emits a `data_quality_warnings` entry instead of
fabricating a 0 ml intake.

## 4. Nutrition persistence, provenance, and import

The `meal` table already carried the macro columns; this upgrade adds
provenance: `sodium_mg`, `source`, `source_record_id`, `is_estimated` (all
nullable). A **calories-only meal is valid**; nutrition totals only ever sum the
values actually present (`nutrition_summary` now also totals `sodium_mg`).

- `Database.add_meal(...)` gains the new fields and returns the row id. When
  `source` + `source_record_id` are supplied, an existing row from the same
  source/record is **updated in place** — imports are idempotent per record.
- `log_meal` and the `log_apple_health_export` batch tool pass the new fields
  through; one malformed record still never fails the batch (per-record status
  is returned).
- The Apple Health XML importer now imports **dietary sodium** (mg/g normalized
  to mg), tags meals `source="apple"`, `is_estimated=True`, and remains
  idempotent (re-import replaces its own tagged rows).

## 5. Data-quality warnings

`detect_workout_quality_warnings` flags physiologically suspicious workout rows
(zero distance on a running/walking activity, implausible average speed) without
deleting them; the context also flags missing hydration and stale (>30d) body
composition. Flags carry the field and a suggested action so a human can correct
the record and sensitive calculations can exclude it.

## 6. Feature-request backlog

`feature_request` table + tools (`create_feature_request`,
`list_feature_requests`, `update_feature_request`) replace free-text profile
notes for tracking requirements. Statuses: `requested | planned | in_progress |
blocked | implemented | rejected`.

---

## Migration report — how existing records are handled

All schema changes here are **additive and lossless**, applied automatically at
startup by the two mechanisms already in place (`Database.init_schema`):

1. **New nullable columns** on `profile` and `meal` (`sleep_*`, `hydration_*`,
   `sodium_mg`, `source`, `source_record_id`, `is_estimated`) are added by the
   generic reconciler `Database._migrate_missing_columns` via
   `ALTER TABLE … ADD COLUMN`, backfilled `NULL`. Existing meals, profiles, and
   their notes are untouched; a calories-only legacy meal stays valid.
2. **New tables** `sleep_target_history` and `feature_request` are created by
   `SQLModel.metadata.create_all`. No existing table is altered.

No Alembic revision is required (no data backfill, index, NOT NULL column, or
rename). The reconciler is idempotent and runs under the existing
cross-container migration lock, so all four services can boot against the one
SQLite file safely.

**Backward compatibility:**
- No public tool was renamed or removed. `generate_daily_plan` is added as an
  alias alongside `get_today_coaching_context`.
- Existing automation prompts keep working; the morning plan is unchanged in
  shape, only better grounded.
- Existing manual/Apple meals and workouts remain readable; provenance columns
  are simply `NULL` on legacy rows.
- Sleep debt changes numerically (7h target, clamped per night) — this is the
  intended correction, documented above and covered by tests.

**Rollback:** every deploy snapshots the DB first
(`data/backups/pre-deploy-*.db`, see `.github/workflows/deploy.yml`), so a
release can be restored to its exact pre-deploy state. The additive columns/
tables are harmless to older code (unread), so a code-only rollback also works
without touching the database.

**Startup verification:** `init_schema` runs `create_all` → column reconciler →
Alembic `upgrade head` → view recreation on every boot; the compose
healthchecks gate on service heartbeats. `build_coaching_context` never raises
on missing data — it reports availability per source.

## Test coverage

`tests/test_coaching_context.py` (plus the updated `tests/test_data.py`) cover:
configurable 7h sleep-debt; per-night clamping; historical sleep calc after a
target change; recovery classification across states; the recommendation ladder;
full context assembly (scheduled-automation entry point); stale-sync refresh and
graceful degradation; missing-hydration-as-unknown; suspicious-workout warnings;
Apple nutrition import with all macros + sodium; calories-only and full-macro
meals; idempotent re-import (both Apple XML and source-record-keyed);
configurable hydration targets; and the feature-request backlog.
