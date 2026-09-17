# Progression analytics

Deterministic, local analytics that answer *"how am I progressing?"* without
asking an LLM to do arithmetic over raw logs. Every number below is computed in
Python from the SQLite database; the MCP handlers are thin wrappers over the
services in `garmin_coach/`.

Shared rules across all progression endpoints:

| Rule | What it means in the response |
| --- | --- |
| Missing is not zero | A metric with no data is `null` plus an `*_unavailable_reason`; a day with no Garmin sync counts as *unknown*, never as confirmed rest. |
| Units are explicit | Every numeric block names its unit (`min_per_km`, `metres per week`, …) and its `direction` (`lower_is_better` / `higher_is_more_volume`). |
| Provenance is attached | Sessions carry `activity_id`, `source` and `load_source`; derived numbers carry `method` and `sample_count`. |
| Canonical sessions count once | Reads exclude `duplicate_of` rows, so a workout recorded by two sources is one session. |
| Windows are calendar days in the user's timezone | Both windows are reported with `start`, `end` and `days`; partial weeks are marked. |

## `get_activity_progress(type, days=90, baseline_days=None, include_sessions=False)`

Generic per-sport progression — `garmin_coach/activity_progress.py`.

`type` accepts an activity key as Garmin records it (`running`, `lap_swimming`,
`cycling`, `walking`, `rowing`, `strength_training`, …). Common spellings are
normalised (`"Running"`, `"road running"`, `"run"` → `running`), but
**materially different modalities are never pooled**: pool vs open-water
swimming, indoor vs outdoor cycling, treadmill vs road running stay separate and
are listed under `related_types_not_included`.

### Response shape

```jsonc
{
  "activity_type": "running",
  "requested_type": "run",
  "family": "run",
  "available": true,
  "as_of": "2026-09-17T08:14:03+03:00",
  "timezone": "Asia/Jerusalem",
  "analysis_window":  {"days": 90, "start": "2026-06-20", "end": "2026-09-17",
                       "boundary": "calendar days in the user timezone, today inclusive"},
  "baseline_window":  {"days": 90, "start": "2026-03-22", "end": "2026-06-19",
                       "selection_method": "the equally long window immediately preceding the analysis window"},
  "related_types_not_included": ["treadmill_running", "trail_running"],
  "supported_metrics": {"distance": true, "pace_unit": "min_per_km",
                        "speed_unit": "kph", "delegates_to": null},

  "summary": {
    "workout_count": 24, "sessions_per_week": 1.87,
    "total_duration_s": 43200.0, "weekly_duration_s": 3360.0,
    "avg_session_duration_s": 1800.0,
    "total_distance_m": 120000.0, "weekly_distance_m": 9333.3,
    "avg_session_distance_m": 5000.0,
    "total_training_load": 1680.0, "weekly_training_load": 130.7,
    "load_sources": ["estimated", "garmin"],
    "avg_hr": 148.2, "avg_hr_method": "duration-weighted mean of per-session average HR",
    "hr_coverage": {"sessions_with_hr": 22, "sessions_total": 24},
    "time_basis": "recorded_duration",
    "excluded_activity_ids": [],
    "pace": {"unit": "min_per_km", "seconds_per_unit": 324.0, "display": "5:24",
             "direction": "lower_is_better",
             "method": "total distance ÷ total recorded_duration over 24 session(s) ≥ 200 m",
             "sample_count": 24, "distance_m": 120000.0, "duration_s": 38880.0},
    "speed": {"unit": "kph", "value": 11.11, "direction": "higher_is_better", …},
    "longest_session": {"activity_id": 123, "day": "2026-08-02", "metric": "distance_m", …}
  },

  "baseline_summary": { /* same shape, over baseline_window */ },
  "comparison": {
    "weekly_distance_m": {"current": 9333.3, "baseline": 7000.0,
                          "absolute_change": 2333.3, "percent_change": 33.3,
                          "direction": "higher_is_more_volume"},
    "pace": {"unit": "min_per_km", "current_seconds_per_unit": 324.0,
             "baseline_seconds_per_unit": 340.0, "absolute_change_s": -16.0,
             "percent_change": -4.7, "direction": "lower_is_better",
             "improved": true, "interpretation": "faster than baseline",
             "caveat": "compare alongside avg_hr and session structure — …"}
  },

  "weekly_series": [{"week_start": "2026-06-22", "week_end": "2026-06-28",
                     "iso_week": "2026-W26", "partial": false, "days_covered": 7,
                     "workout_count": 2, "duration_s": 3600.0,
                     "distance_m": 10000.0, "training_load": 140.0}, …],
  "trends": {"sample_weeks": 12, "excluded_partial_weeks": 1,
             "method": "least-squares slope over complete calendar weeks",
             "available": true,
             "distance_m": {"slope_per_week": 420.5, "unit": "metres per week",
                            "direction": "increasing"}, …},
  "records": {"basis": "whole-session averages over canonical workouts",
              "caveat": "session-level observations, not continuous-effort personal records; …",
              "longest_distance_m": {…}, "longest_duration_s": {…},
              "best_session_pace": {…}},
  "coverage": {"window_days": 90, "synced_days": 88, "unsynced_days": 2,
               "coverage_ratio": 0.978, "unsynced_day_list": ["2026-07-04", …],
               "note": "days without a recorded sync are unknown, not confirmed rest days"},
  "data_quality": {"warnings": [...], "excluded_from_distance_and_pace": [],
                   "note": "flagged sessions still count towards frequency and volume …"},
  "sessions_included": 24
}
```

### Formulas

| Metric | Formula | Notes |
| --- | --- | --- |
| `sessions_per_week` | `workout_count ÷ (window_days / 7)` | Window length, not observed weeks. |
| `weekly_*` | `total ÷ (window_days / 7)` | Same denominator for every weekly figure. |
| `pace.seconds_per_unit` | `total_time ÷ (total_distance / unit_metres)` | **Weighted**: totals over totals, never the mean of per-session paces. Sessions below `MIN_PACE_DISTANCE_M` (200 m) are excluded from the numerator *and* denominator. |
| `speed.value` | `total_distance ÷ total_time × 3.6` | km/h. |
| `avg_hr` | `Σ(avg_hr × duration) ÷ Σ duration` | Duration-weighted; `hr_coverage` reports how many sessions carried HR. |
| `percent_change` | `(current − baseline) ÷ |baseline| × 100` | `null` when either side is missing **or the baseline is zero**. |
| `trends.*.slope_per_week` | Least-squares slope of the weekly value over the week index | Partial weeks excluded from the fit; `sample_weeks` reports the count. |

`time_basis` says which clock the pace used: `active_duration` when rows carry a
separately recorded moving time (swim detail ingestion supplies it), otherwise
`recorded_duration`.

### Limitations

* Session-level records are **observations**, not continuous-effort PRs. A
  continuous PR needs contiguous split/length data and is only reported by the
  swimming service where that data exists.
* A faster average at a higher average heart rate is a harder effort, not proof
  of improved aerobic fitness — the `pace` comparison carries that caveat and
  the HR context beside it.
* Strength (`family: "strength"`) reports frequency, duration and load only; its
  exercise-level progression belongs to `get_strength_progress`. No distance or
  pace is invented for it.
* Windows are bounded at `MAX_WINDOW_DAYS` (730) per call, and detailed session
  output is opt-in (`include_sessions`) and capped.

## `get_recorded_activity_types(days=90)`

Every activity type actually recorded in the window with its canonical key,
family, session count and the raw type strings seen. The combined progress
report iterates this so a sport the user does (a swim, a row) can never be
silently omitted because nobody asked for it by name.

## `get_swimming_progress(days=90, pool_length_m=None, stroke=None, min_distance_m=None, include_sessions=False)`

First-class swim analytics — `garmin_coach/swimming.py`, backed by the swim
detail ingested into `activity_detail` / `activity_length`.

A swim summary alone cannot say whether the swimmer got faster: an
equal-distance session can be quicker because the swimming was faster, because
the rests were shorter, or because the effort was higher. The report keeps those
apart.

### Ingestion

Garmin's per-day activity list is summaries only (one duration, no lengths, no
rest). `GarminClient._pull_activity_detail` makes a second, per-activity call for
swim types and stores:

* `activity_detail` — one row per activity asked about, with the **separate**
  `elapsed_duration_s`, `timer_duration_s` and `active_duration_s` clocks,
  `rest_duration_s` + `rest_source`, pool length (raw value, unit, and metres
  only when the unit is known), primary stroke and provider session averages.
  `status` is `ok` / `empty` / `unsupported` / `error`.
* `activity_length` — one row per recorded length/lap, with `is_rest` marked.

Properties this buys:

| Property | How |
| --- | --- |
| Daily ingestion never breaks | The detail call is wrapped per activity; a failure logs and records `status="error"`, the summary write already happened. |
| Backfill is bounded and resumable | `backfill_swim_details(days, limit, retry_errors)` asks about at most `limit` activities and skips any already asked about, whatever the answer. `remaining` reports what's left. |
| Re-sync is idempotent | The detail row is a field-preserving upsert; lengths are **replaced**, never appended. |
| Unsupported providers degrade | Capability detection (`get_activity`, `get_activity_typed_splits` / `get_activity_splits`); a client exposing neither records `status="unsupported"` once and is not re-asked. |

### Time semantics

Elapsed, timer and active time are **not interchangeable**. Rest is only
produced from a documented compatible pair:

1. the sum of provider-marked rest intervals, or
2. `elapsed_duration − active_duration` when both exist.

Otherwise `rest_duration_s` is `null` with a reason, and
`active_time_available: false` means no active pace, SWOLF, stroke efficiency or
continuous PR is reported for that session.

### Continuous-effort bests

`best_continuous_effort` slides a window over each **contiguous run** of
adjacent, non-rest lengths in one stroke (a rest, a stroke change or a gap in
`length_index` ends a run) and only accepts a window whose summed length
distance equals the target *exactly*. So:

* a 25 m pool supports 50/100/200/400 m; a 33 m pool does **not** support 100 m;
* two 50 m blocks either side of a rest never become a 100 m PR;
* a session with no length data supports **no** continuous bests, and says so.

### Comparability

Efficiency is grouped by pool length *as recorded* (value **and** unit) plus
stroke — a 25 yd session and a 25 m session are never pooled, and neither are
different strokes. Pool and open-water swims are reported separately under
`by_modality`. Distance totals convert yards to metres; efficiency metrics do
not pool across pools.

### Limitations

* SWOLF and strokes-per-length are only comparable within one pool length and
  stroke; the response repeats that caveat next to the value.
* A faster active pace at a higher average heart rate is a harder effort, not
  proof of improved fitness — `comparison.interpretation_note` says so and the
  HR comparison sits beside the pace comparison.
* Sessions recorded before detail ingestion existed report elapsed time only
  until `backfill_swim_details` has run over them; they are listed under
  `data_quality.sessions_without_detail_ingested`.

## `get_strength_progress(exercise=None, days=120, include_sessions=True)`

Longitudinal strength progression — `garmin_coach/strength_progress.py`, built
on the existing strength tables and `exercise_metrics.normalize_performance`
(so a legacy `"3"`, a rep range `"10-12"` or a JSON list degrades to `None` with
a data-quality note instead of crashing or being guessed at).

With `exercise` set it returns the per-session history, records, progression and
data-quality report for one lift. Without it, a bounded per-exercise summary of
everything trained in the window.

### Volume

| Case | `volume_basis` | Formula |
| --- | --- | --- |
| Per-set data recorded | `per_set` | `Σ(reps × weight)` over the recorded sets — **exact**, so 10×60 + 8×70 + 6×80 = 1640 kg, not 80 × 24. |
| Aggregate columns only | `aggregate_estimate` | Carried from `exercise_history` and labelled; sets at different weights are not distinguishable in such a record. |
| Skipped / substituted | `null` | Excluded from completed volume entirely. |

A set whose reps or weight cannot be read is not counted as completed work.

### Top set vs working weight

`top_set_weight_kg` is the heaviest set; `working_weight_kg` is the weight
carried across **every** working set; `all_sets_at_top_weight` distinguishes the
two. A weight record reports its `scope` accordingly — *"carried across every
working set"* vs *"top set only"* — so adding a heavier single is never
presented as moving the whole session up.

### Records

Observed over the user's own logged sessions only:
`heaviest_weight_kg`, `highest_volume_kg`, `most_reps_in_a_set` (with the note
that more reps at a lighter weight is not a heavier lift) and
`best_estimated_1rm`. Sessions that were skipped, substituted or whose stored
values couldn't be read are listed under `excluded_sessions` — reported, not
silently dropped.

### Estimated 1RM

Epley, `weight × (1 + reps / 30)`, produced **only** from a completed set of
1–10 reps and always flagged `is_estimate: true` with the formula named. Outside
that range the value is `null` with a reason. It is not a measured maximum.

### Aliases, equipment and load conventions

Normalization folds case, spacing, punctuation and a short list of unambiguous
abbreviations (`DB` → dumbbell, `OHP` → overhead press, `pull-ups` → pullup).
Plural folding is deliberately timid (`press` and `lats` keep their ending).
Nothing that changes the movement or the equipment folds: *incline dumbbell
press* ≠ *dumbbell press*, and the same movement on two machines is two
progressions (`equipment_variants` + an explicit note).

`load_convention` names how the number should be read:

| Convention | Meaning |
| --- | --- |
| `per_hand` | Dumbbell/kettlebell — not comparable with a barbell total. |
| `total_load` | Total external load including the bar. |
| `machine_stack` | Specific to that machine's leverage; never comparable with free weights or a different machine. |
| `bodyweight` | Any recorded weight is *added* load. |
| `assisted` | A larger number means an *easier* set. |
| `unknown` | The log doesn't say — reported as recorded, never assumed. |

### Progression

Change from the earliest to the latest usable session in the window (absolute,
percentage, with dates). `percent_change` is withheld on a zero or missing
baseline. A `rate` (least-squares slope of top-set weight per week) needs at
least `MIN_SESSIONS_FOR_RATE` (3) usable sessions; below that it is `null` with
a reason rather than noise. Fewer than two sessions → no progression claimed at
all.

### Limitations

* Records are the user's own observed bests in the window — not population
  rankings, not forecasts.
* A volume change that mixes exact per-set sessions with aggregate estimates is
  flagged `mixed_basis` with a caveat.
* Existing `get_exercise_history` and `recommend_next_weights` are untouched and
  remain the write/recommendation path.

## `get_workout_data_quality(days=90)`

Read-only data-quality report — `garmin_coach/workout_quality.py`.

This is the **single** warning pipeline. The detector that
`coaching_context.detect_workout_quality_warnings` exposed (zero distance,
implausible speed) is now a legacy-shaped wrapper over `detect_findings`, so
there is one place to extend and no competing pipelines.

### Finding types

| Type | Severity | Meaning |
| --- | --- | --- |
| `near_zero_duration` | warning | A recording shorter than `NEAR_ZERO_DURATION_S` (120 s) — a mis-start, not a session. |
| `partial_recording` | critical | A recording covering < `PARTIAL_COVERAGE_RATIO` (50%) of the session another source recorded. Its HR/calories/load describe a slice. |
| `zero_distance` | warning | A distance sport of > 5 min with no distance. |
| `impossible_speed` | critical | Average speed above ~45 km/h on a non-cycling activity. |
| `impossible_time_distance` | warning | A distance with no duration — no pace derivable. |
| `missing_essential_data` | warning | Neither duration nor distance recorded. |
| `source_field_mismatch` | warning | Two sources of one session disagree materially about a field. |
| `unresolved_match_candidate` | info/warning | Two records that *may* be one session, left separate for want of evidence. |

Every finding carries `evidence` (the actual numbers), `related_activity_ids`,
a `suggested_action` and `blocks_records`.

### Matching is evidence-based

`_plausibly_same_session` requires different sources and a compatible type;
timing and duration then raise or lower `match_confidence`. **A shared date is
never sufficient** — two runs twelve hours apart are reported as an unresolved
candidate with their evidence, never merged. Resolving one is an explicit,
separate call (`merge_workout_sources(source_activity_ids=[…])`), reversible
with `unmerge_workout_sources`.

### Quality-aware merging

`workout_quality.source_quality(sources)` marks a source *incomplete* when its
duration covers less than half the longest duration recorded for the same
session. `workout_merge.merge_fields(sources, is_strength, quality=…)` then
demotes an incomplete source below any complete one for the **whole-session**
domains — `duration`, `calories`, `training_load` — whatever the usual domain
priority says. So for the issue's example:

| Field | Without quality awareness | With it |
| --- | --- | --- |
| `duration_s` | 11 s (Garmin, by priority) | 3300 s (manual) |
| `calories` | 3 | 320 |
| `training_load` | 1.0 | 75.0 |
| `avg_hr` | 92 (Garmin) | 92 (Garmin) — **kept**, annotated `covers: "partial"` |

Partial physiology is preserved rather than discarded: it is real data about the
slice it covers. `coverage_annotations` records the per-field coverage, it is
stored on the canonical's `meta_json`, and `get_merged_workout` surfaces it as
`field_coverage`. A *complete* Garmin recording still wins physiology, duration
and load exactly as before — quality awareness only fires on demonstrably
incomplete data.

### Feeding the progression APIs

`record_blocking_ids(findings)` is the set of sessions that cannot create a
record or a headline total. `get_activity_progress` excludes them from totals,
pace and records, and reports them under
`data_quality.excluded_from_distance_and_pace` plus `records.excluded_activity_ids`
— exclusions are reported, never silent data loss.

### Read-only by construction

`get_workout_data_quality` takes rows and returns findings. It never merges,
never force-merges and never writes; a test asserts storage is byte-identical
before and after. Applying a fix is a separate, explicit call with `dry_run`
available to preview it.

## `get_sport_training_load(days=28, sport=None)` / `get_training_load(days, by_sport=True)`

Per-sport decomposition of the training-load metric — `garmin_coach/sport_load.py`.

`get_training_load(days)` is unchanged: same keys, same acute:chronic numbers.
`by_sport=True` adds a `sport_breakdown` block; the dedicated tool returns that
block alone.

### It is a decomposition, not a second metric

Per-sport load is the same `Workout.training_load` column, bucketed with the
same normalizer `get_activity_progress` uses. So:

* `reconciliation.sum_of_sport_load == reconciliation.reported_total_load`
  (asserted by a test, and reported as `reconciles` in every unfiltered call);
* the per-sport acute:chronic pair uses the **same** EWMA spans (7 d / 28 d) and
  the same seeding rule as `Analyzer.acute_chronic_ratio`, so a single-sport
  history produces the same ratio in both views;
* anything not on that scale — strength working sets, kg × reps volume — lives
  in a separate `strength_workload` block listed under
  `reconciliation.not_summed` and is never added in.

### Per sport

| Field | Notes |
| --- | --- |
| `session_count`, `sessions_per_week` | Canonical workouts only; merged sessions count once. |
| `total_duration_s`, `weekly_duration_s` | Always available. |
| `total_distance_m`, `weekly_distance_m` | `null` + `distance_unavailable_reason` where distance isn't meaningful. |
| `total_training_load`, `weekly_training_load` | The bucketed legacy metric. |
| `load_by_source` | `garmin` (device EPOC) vs `estimated` (documented heuristic) vs `manual`, with session counts and `sessions_without_load`. Different methods, reported separately. |
| `acute_chronic` | Per-sport EWMA pair + ratio, or `unavailable_reason`. |
| `coverage` | Sessions with a load value vs total — a session without one is **missing data, not zero load**. |
| `excluded_activity_ids` | Recordings the quality detector flagged as unusable. |

A sport whose own history spans fewer than `SPORT_MIN_HISTORY_DAYS` (21) days,
or whose chronic load is zero, returns `ratio: null` with the reason — never a
divide by zero and never a number borrowed from the overall series.

### Strength

`strength_workload` reports session frequency, `completed_working_sets`,
`exercises_performed`, `completed_volume_kg` (exact per-set where the data
supports it, `volume_basis` says which) and `weekly_volume_kg`, with units
spelled out. It is computed from the logged sets, so **strength workload stays
visible when the device recorded no HR and no load at all**.

### Rest vs missing sync

`rest_and_coverage` separates:

* `active_days` — a session was recorded;
* `confirmed_rest_days` — the day synced successfully and had no session;
* `unknown_days` — no recorded sync at all, so nothing can be concluded.

### Limitations

* Acute:chronic is a descriptive monitoring signal over the user's own history —
  **not** an injury prediction and not a universal safe/unsafe threshold. Every
  ratio carries that note.
* Optional session-RPE × duration is deliberately *not* synthesised: the repo's
  estimator already documents its inputs, and inventing an RPE or deriving a
  full-session load from a truncated recording is exactly what the quality
  detector exists to prevent.

## `get_training_progress_report(days=56, baseline_days=None, detail=False)`

The default answer source for *"how am I progressing?"* —
`garmin_coach/progress_report.py`. Composed from the deterministic services
above, not from an LLM re-deriving numbers from raw logs.

### Sections

| Section | Contents |
| --- | --- |
| `sports` | One section per activity type **actually recorded** in the window (so swimming or another major sport can never be silently omitted), each with summary, baseline summary, comparison and observed records. |
| `swimming` | The full swimming report when any swim exists, else `available: false` with a reason. |
| `strength` | Per-exercise records, progression and load conventions. |
| `adherence` | `TrainingPlan` statuses with an explicit denominator. |
| `recovery` | Reused from `Analyzer.report` plus the latest readiness check-in. |
| `notable_prs` | Each labelled `observation` or `estimate`, with `basis` and source ids. |
| `concerns` | Declines, effort-confounded improvements, thin samples, incomplete recordings, unresolved reconciliation, sparse coverage. |
| `data_quality` | The quality roll-up plus the ids excluded from totals and records. |

### Consistency by construction

The `sports` sections are built from one `recent_workouts` scan through the
**same** `activity_progress` primitives (`summarize_sessions`, `compare`,
`observed_records`) that `get_activity_progress` uses. A test asserts the
report's running section equals the per-sport endpoint's numbers — count,
distance, pace and records — for the same window. `detail=True` adds the full
per-sport reports, the per-exercise strength history and the sport load
breakdown.

### Adherence

```
completion_rate_pct = (completed + 0.5 × partially_completed) ÷ resolved × 100
```

`resolved` = done + partially_done + skipped. Plans still marked `planned` are
excluded from the rate and reported as `still_open`. **No recorded plan means
adherence is `available: false` with a reason — never 0%.**

### What it will not say

* No population rankings and no forecasts — the comparison is the user's own
  preceding window of equal length, with its selection method stated.
* No blended *"overall fitness +X%"*: unrelated lifts and sports are never
  collapsed into one number.
* Recovery is context, not a cause: the report never claims a recovery metric
  explains a performance change.
* A faster pace at a higher heart rate is reported as an observation **and**
  raised as a concern (`effort_confounded_improvement`), not as proof of
  improved fitness.

### Reuse in coaching

`progress_report_summary()` is the bounded form. `build_coaching_context(...,
include_progress_summary=True)` — and `get_today_coaching_context(
include_progress_summary=True)` — attach it under `progress_summary`, so the
coach cites these deterministic numbers instead of re-deriving trends. It is off
by default so the 07:30 plan job stays cheap, and it is failure-isolated: a
problem building it records an error in that key rather than costing the daily
plan its context.
