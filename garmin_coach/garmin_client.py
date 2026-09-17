"""Garmin Connect puller.

Resumes an authenticated session from cached tokens (written once by
``scripts/garmin_login.py``) and pulls a day's metrics into the database. Garmin's
undocumented JSON varies by device and firmware, so every extractor is defensive:
missing keys yield ``None`` rather than raising, and per-metric failures are logged
and skipped so one bad endpoint never aborts the whole pull.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any, Callable

from garminconnect import Garmin

from .config import settings
from .database import Database
from .swimming import pool_length_meters
from .training_load import estimate_training_load

log = logging.getLogger("garmin_coach.garmin")

# Pause between multi-day pulls (backfill, gap healing). Each day is ~9 API
# calls; hammering them back-to-back is what gets long backfills rate-limited
# and killed mid-run.
PULL_PAUSE_SECONDS = 1.0


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    """Safely walk nested dict/list keys, returning ``default`` on any miss."""
    cur = d
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return default
        if cur is None:
            return default
    return cur if cur is not None else default


class GarminClient:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()
        self.api: Garmin | None = None

    def login(self) -> None:
        """Resume a session from the cached token store.

        Credentials are passed as a fallback so garminconnect can transparently
        re-authenticate and re-cache tokens in its current format if the stored
        ones are rejected (e.g. after a garminconnect upgrade that changes the
        token format) rather than failing outright.
        """
        self.api = Garmin(
            email=settings.garmin_email or None,
            password=settings.garmin_password or None,
        )
        self.api.login(settings.garmin_token_dir)
        log.info("Authenticated to Garmin Connect via cached tokens.")

    def ensure_login(self) -> None:
        if self.api is None:
            self.login()

    # ── Per-metric extractors ──────────────────────────────────────────────────

    def _pull_sleep(self, day: str) -> None:
        data = self.api.get_sleep_data(day)
        dto = _get(data, "dailySleepDTO", default={})
        if not dto:
            return
        self.db.upsert_sleep(
            day,
            score=_get(dto, "sleepScores", "overall", "value"),
            total_seconds=_get(dto, "sleepTimeSeconds"),
            deep_seconds=_get(dto, "deepSleepSeconds"),
            light_seconds=_get(dto, "lightSleepSeconds"),
            rem_seconds=_get(dto, "remSleepSeconds"),
            awake_seconds=_get(dto, "awakeSleepSeconds"),
            resting_hr=_get(data, "restingHeartRate"),
        )

    def _pull_hrv(self, day: str) -> None:
        data = self.api.get_hrv_data(day)
        summary = _get(data, "hrvSummary", default={})
        if not summary:
            return
        self.db.upsert_hrv(
            day,
            last_night_avg=_get(summary, "lastNightAvg"),
            weekly_avg=_get(summary, "weeklyAvg"),
            status=_get(summary, "status"),
        )

    def _pull_resting_hr(self, day: str) -> None:
        data = self.api.get_rhr_day(day)
        metrics = _get(
            data, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE",
            default=[],
        )
        value = metrics[0].get("value") if metrics else None
        if value is None:
            return
        self.db.upsert_resting_hr(day, resting_hr=int(value))

    def _pull_stress(self, day: str) -> None:
        data = self.api.get_stress_data(day)
        avg = _get(data, "avgStressLevel")
        if avg is None or avg < 0:
            return
        self.db.upsert_stress(
            day,
            avg_stress=avg,
            max_stress=_get(data, "maxStressLevel"),
            rest_seconds=_get(data, "restStressDuration"),
        )

    def _pull_body_battery(self, day: str) -> None:
        data = self.api.get_body_battery(day, day)
        if not data:
            return
        entry = data[0] if isinstance(data, list) else data
        self.db.upsert_body_battery(
            day,
            high=_get(entry, "highestBatteryLevel"),
            low=_get(entry, "lowestBatteryLevel"),
            charged=_get(entry, "charged"),
            drained=_get(entry, "drained"),
        )

    def _pull_steps(self, day: str) -> None:
        data = self.api.get_user_summary(day)
        steps = _get(data, "totalSteps")
        if steps is None:
            return
        self.db.upsert_steps(
            day,
            steps=steps,
            goal=_get(data, "dailyStepGoal"),
            distance_m=_get(data, "totalDistanceMeters"),
            calories=_get(data, "totalKilocalories"),
            floors_climbed=_get(data, "floorsAscended"),
        )

    def _pull_weight(self, day: str) -> None:
        data = self.api.get_weigh_ins(day, day)
        entries = _get(data, "dailyWeightSummaries", default=[])
        if not entries:
            return
        latest = _get(entries[0], "latestWeight", default={})
        if not latest:
            return
        grams = _get(latest, "weight")
        self.db.upsert_weight(
            day,
            weight_kg=round(grams / 1000.0, 2) if grams else None,
            body_fat=_get(latest, "bodyFat"),
            muscle_kg=(
                round(_get(latest, "muscleMass") / 1000.0, 2)
                if _get(latest, "muscleMass") else None
            ),
            body_water=_get(latest, "bodyWater"),
            bmi=_get(latest, "bmi"),
        )

    def _pull_hydration(self, day: str) -> None:
        data = self.api.get_hydration_data(day)
        intake = _get(data, "valueInML")
        if intake is None:
            return
        self.db.upsert_hydration(
            day,
            intake_ml=int(intake),
            goal_ml=int(_get(data, "goalInML", default=0)) or None,
        )

    def _pull_workouts(self, day: str) -> None:
        activities = self.api.get_activities_by_date(day, day) or []
        for act in activities:
            aid = _get(act, "activityId")
            if aid is None:
                continue
            load = _get(act, "activityTrainingLoad")
            wtype = _get(act, "activityType", "typeKey")
            duration = _get(act, "duration")
            avg_hr = _get(act, "averageHR")
            load_source = "garmin"
            if load is None:
                # Garmin omits load on some activities (walks, manual entries);
                # estimate so the acute:chronic math doesn't read them as rest.
                load = estimate_training_load(wtype, duration, avg_hr=avg_hr)
                load_source = "estimated" if load is not None else None
            self.db.upsert_workout(
                int(aid),
                day,
                name=_get(act, "activityName"),
                type=wtype,
                duration_s=duration,
                distance_m=_get(act, "distance"),
                calories=_get(act, "calories"),
                avg_hr=avg_hr,
                max_hr=_get(act, "maxHR"),
                training_load=load,
                source="garmin",
                load_source=load_source,
                start_time=_get(act, "startTimeLocal"),
            )
            # Swims need per-length detail to say anything truthful about
            # active pace, rest or stroke efficiency. Isolated per activity so
            # a detail failure never costs us the day's summary ingestion.
            if (wtype or "").lower() in self.DETAIL_TYPES:
                try:
                    self._pull_activity_detail(int(aid), day, wtype)
                except Exception as exc:  # noqa: BLE001 — summary wins
                    log.warning("Swim detail skipped for %s: %s", aid, exc)

    # ── Per-activity detail (swim lengths / splits) ────────────────────────────
    # The daily activity list is summaries only: one duration, no lengths, no
    # rest. Truthful swim analytics needs the separate clocks (elapsed / timer /
    # moving) and the per-length records, which live behind a second call per
    # activity. That call is expensive and not supported by every
    # garminconnect/device combination, so it is:
    #   * only made for activity types that actually benefit (swims today),
    #   * wrapped so a failure never aborts the day's summary ingestion, and
    #   * recorded either way (status ok/empty/unsupported/error) so the
    #     historical backfill is resumable and never re-asks the same questions.

    DETAIL_TYPES = ("lap_swimming", "open_water_swimming", "swimming")

    @staticmethod
    def _swim_lengths(payload: Any) -> list[dict[str, Any]]:
        """Per-length rows from Garmin's typed-splits payload.

        Pool swims come back as ``lengthDTOs``; some firmwares only provide
        ``lapDTOs``. Both are walked defensively — a shape we don't recognise
        yields no lengths rather than a wrong one. A split the provider itself
        typed as a rest interval is kept and marked, because rest is exactly
        what separates active-pace gains from shorter recoveries.
        """
        if not isinstance(payload, dict):
            return []
        raw = (
            payload.get("lengthDTOs")
            or payload.get("lengths")
            or payload.get("lapDTOs")
            or []
        )
        if not isinstance(raw, list):
            return []
        out: list[dict[str, Any]] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            split_type = (
                _get(entry, "lengthType")
                or _get(entry, "swimLengthType")
                or _get(entry, "intensityType")
                or _get(entry, "type")
            )
            stroke = _get(entry, "swimStroke") or _get(entry, "strokeType")
            is_rest = bool(
                str(split_type or "").upper().find("REST") >= 0
                or str(stroke or "").upper() == "REST"
                or _get(entry, "activeLength") is False
            )
            out.append({
                "length_index": _get(entry, "lengthIndex", default=index + 1),
                "start_time": _get(entry, "startTimeLocal") or _get(entry, "startTimeGMT"),
                "duration_s": _get(entry, "duration") or _get(entry, "elapsedDuration"),
                "distance_m": _get(entry, "distance"),
                "stroke": stroke,
                "strokes": _get(entry, "totalStrokes") or _get(entry, "strokes"),
                "swolf": _get(entry, "swolf") or _get(entry, "avgSwolf"),
                "avg_hr": _get(entry, "averageHR"),
                "is_rest": is_rest,
                "split_type": split_type,
            })
        return out

    def _pull_activity_detail(self, activity_id: int, day: str, activity_type: str | None) -> str:
        """Fetch and store one activity's detail. Returns the recorded status.

        Never raises: a provider failure is recorded as ``error`` (so the
        backfill can retry it deliberately) and the caller carries on.
        """
        summary_fn = getattr(self.api, "get_activity", None)
        splits_fn = getattr(self.api, "get_activity_typed_splits", None) or getattr(
            self.api, "get_activity_splits", None
        )
        if summary_fn is None and splits_fn is None:
            self.db.upsert_activity_detail(
                activity_id, day, activity_type=activity_type, status="unsupported",
                error="installed garminconnect exposes no activity-detail endpoint",
            )
            return "unsupported"

        fields: dict[str, Any] = {"activity_type": activity_type}
        sources: list[str] = []
        try:
            summary = summary_fn(activity_id) if summary_fn else None
            if summary:
                sources.append("get_activity")
                dto = _get(summary, "summaryDTO", default={}) or summary
                fields.update(
                    elapsed_duration_s=_get(dto, "elapsedDuration"),
                    timer_duration_s=_get(dto, "duration"),
                    active_duration_s=_get(dto, "movingDuration"),
                    pool_length_raw=_get(dto, "poolLength"),
                    pool_length_unit=_get(summary, "unitOfPoolLength", "unitKey")
                    or _get(dto, "unitOfPoolLength", "unitKey"),
                    total_lengths=_get(dto, "numberOfActiveLengths")
                    or _get(dto, "totalNumberOfLengths"),
                    total_strokes=_get(dto, "strokes") or _get(dto, "totalStrokes"),
                    avg_swolf=_get(dto, "averageSwolf") or _get(dto, "avgSwolf"),
                    avg_stroke_distance_m=_get(dto, "avgStrokeDistance"),
                    avg_stroke_rate=_get(dto, "averageSwimCadenceInStrokesPerMinute")
                    or _get(dto, "avgStrokeCadence"),
                    primary_stroke=_get(dto, "swimStroke") or _get(summary, "swimStroke"),
                )
            lengths = self._swim_lengths(splits_fn(activity_id)) if splits_fn else []
            if lengths:
                sources.append(splits_fn.__name__)
        except Exception as exc:  # noqa: BLE001 — detail must never break a pull
            log.warning("Activity detail failed for %s: %s", activity_id, exc)
            self.db.upsert_activity_detail(
                activity_id, day, activity_type=activity_type,
                status="error", error=str(exc)[:500],
            )
            return "error"

        fields["pool_length_m"] = pool_length_meters(
            fields.get("pool_length_raw"), fields.get("pool_length_unit")
        )
        stored = self.db.replace_activity_lengths(activity_id, lengths)
        active_lengths = sum(1 for l in lengths if not l["is_rest"])
        has_payload = stored > 0 or any(
            fields.get(k) is not None
            for k in ("elapsed_duration_s", "active_duration_s", "pool_length_raw")
        )
        self.db.upsert_activity_detail(
            activity_id, day,
            status="ok" if has_payload else "empty",
            detail_source="+".join(sources) or None,
            length_count=stored,
            active_lengths=active_lengths or None,
            **{k: v for k, v in fields.items() if v is not None},
        )
        return "ok" if has_payload else "empty"

    def pull_activity_details(
        self,
        days: int = 365,
        limit: int = 25,
        types: tuple[str, ...] | None = None,
        retry_errors: bool = False,
    ) -> dict[str, Any]:
        """Bounded, resumable historical backfill of per-activity detail.

        Asks about at most ``limit`` activities per run, skipping any already
        asked about, so a long history fills in gradually without re-triggering
        the rate limiting that tends to kill big backfills.
        """
        self.ensure_login()
        pending = self.db.activities_missing_detail(
            types=list(types or self.DETAIL_TYPES),
            days=days, limit=limit, retry_errors=retry_errors,
        )
        results: dict[str, int] = {}
        processed: list[dict[str, Any]] = []
        for index, workout in enumerate(pending):
            if index:
                time.sleep(PULL_PAUSE_SECONDS)
            status = self._pull_activity_detail(
                workout["activity_id"], workout["day"], workout.get("type")
            )
            results[status] = results.get(status, 0) + 1
            processed.append({"activity_id": workout["activity_id"], "status": status})
        remaining = len(
            self.db.activities_missing_detail(
                types=list(types or self.DETAIL_TYPES), days=days, limit=10_000
            )
        )
        return {
            "requested_limit": limit,
            "processed": processed,
            "status_counts": results,
            "remaining": remaining,
            "resumable": True,
        }

    # ── Orchestration ──────────────────────────────────────────────────────────

    def pull_day(self, day: str | date) -> dict[str, str]:
        """Pull every metric family for ``day``. Returns per-metric status."""
        self.ensure_login()
        day_str = day.isoformat() if isinstance(day, date) else str(day)[:10]
        pulls: dict[str, Callable[[str], None]] = {
            "sleep": self._pull_sleep,
            "hrv": self._pull_hrv,
            "resting_hr": self._pull_resting_hr,
            "stress": self._pull_stress,
            "body_battery": self._pull_body_battery,
            "steps": self._pull_steps,
            "weight": self._pull_weight,
            "hydration": self._pull_hydration,
            "workouts": self._pull_workouts,
        }
        results: dict[str, str] = {}
        for name, fn in pulls.items():
            try:
                fn(day_str)
                results[name] = "ok"
            except Exception as exc:  # noqa: BLE001 — one bad metric must not abort
                log.warning("Pull failed for %s on %s: %s", name, day_str, exc)
                results[name] = f"error: {exc}"
        # Record the day as pulled only if something succeeded: an all-error
        # day (auth broken, Garmin down) should stay "missing" and be retried
        # by gap healing, while a day the watch simply had no data should not.
        if any(status == "ok" for status in results.values()):
            self.db.record_pull(day_str, results)
        log.info("Pulled %s → %s", day_str, results)
        return results

    def pull_range(self, start: date, end: date) -> None:
        """Pull every day in ``[start, end]`` inclusive (used by backfill)."""
        self.ensure_login()
        current = start
        while current <= end:
            self.pull_day(current)
            current += timedelta(days=1)
            if current <= end:
                time.sleep(PULL_PAUSE_SECONDS)

    def pull_missing_days(self, window_days: int | None = None, limit: int = 7) -> list[str]:
        """Heal gaps: pull up to ``limit`` of the oldest days in the backfill
        window that have no successful pull recorded.

        This is what actually recovers from an interrupted backfill — the
        boot-time backfill only runs when the DB is completely empty, and the
        daily pull only covers yesterday and today, so without this a hole in
        the middle of the history would never get filled. Bounded per call so
        the nightly job heals gradually instead of re-triggering the rate
        limiting that likely caused the gap.
        """
        window_days = window_days or settings.backfill_days
        end = date.today()
        start = end - timedelta(days=window_days)
        expected = [
            (start + timedelta(days=i)).isoformat()
            for i in range((end - start).days + 1)
        ]
        pulled = self.db.pulled_days(expected[0], expected[-1])
        missing = [d for d in expected if d not in pulled][:limit]
        if not missing:
            return []
        log.info("Healing %d missing day(s): %s", len(missing), missing)
        self.ensure_login()
        for i, day in enumerate(missing):
            if i:
                time.sleep(PULL_PAUSE_SECONDS)
            self.pull_day(day)
        return missing

    def backfill(self, days: int | None = None) -> None:
        days = days or settings.backfill_days
        end = date.today()
        start = end - timedelta(days=days)
        log.info("Backfilling %s days: %s → %s", days, start, end)
        self.pull_range(start, end)
