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
