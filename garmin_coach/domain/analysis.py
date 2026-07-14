"""Trend analyzer — runs before any LLM call.

Turns raw daily rows into the comparisons a coach actually reasons about: 7-day
vs 28-day baselines, sleep debt, an acute:chronic training-load ratio, and a set
of plain-language flags ("HRV down 3 days straight"). The output is a compact
dict that gets injected into the coach prompt, so the LLM spends its budget on
advice rather than arithmetic.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import mean
from typing import Any

from garmin_coach.storage.database import Database

# Don't compute an acute:chronic ratio until the data spans at least this many
# days — with a short history the 28-day denominator is mostly imaginary, and a
# brand-new database would flag a "training spike" on day one.
_ACR_MIN_HISTORY_DAYS = 21

# Fetch extra history beyond the 28-day chronic span so the chronic EWMA has
# warmed up past its seed value by the time it reaches today.
_ACR_WARMUP_DAYS = 56


def _avg(values: list[float | int | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return round(mean(clean), 2) if clean else None


def _delta(recent: float | None, baseline: float | None) -> float | None:
    if recent is None or baseline is None:
        return None
    return round(recent - baseline, 2)


def _series(rows: list[dict[str, Any]], key: str) -> list[float | None]:
    return [r.get(key) for r in rows]


def _ewma(values: list[float], span_days: int) -> float:
    """Final exponentially weighted moving average over a daily series.

    Standard span form (λ = 2/(N+1)), seeded on the first value so a steady
    series converges to itself instead of dragging a zero seed around.
    """
    lam = 2.0 / (span_days + 1)
    ewma = values[0]
    for v in values[1:]:
        ewma = v * lam + ewma * (1.0 - lam)
    return ewma


def _window(rows: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    """Rows from the last ``days`` *calendar* days (today inclusive).

    The summary only has rows for days with data, so slicing ``rows[-7:]``
    would silently stretch "the last week" across an arbitrary time span
    whenever days are missing. Filtering on the day column keeps windows
    honest.
    """
    cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
    return [r for r in rows if (r.get("day") or "") >= cutoff]


class Analyzer:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    def _trend(self, rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
        """7-day vs 28-day average and their delta for a single metric."""
        last7 = _avg(_series(_window(rows, 7), key))
        last28 = _avg(_series(_window(rows, 28), key))
        return {"avg_7d": last7, "avg_28d": last28, "delta": _delta(last7, last28)}

    def _consecutive_decline(self, rows: list[dict[str, Any]], key: str) -> int:
        """Count trailing days where the metric strictly decreased."""
        vals = [r.get(key) for r in rows if r.get(key) is not None]
        streak = 0
        for i in range(len(vals) - 1, 0, -1):
            if vals[i] < vals[i - 1]:
                streak += 1
            else:
                break
        return streak

    def sleep_debt(
        self, rows: list[dict[str, Any]], target_hours: float | None = None
    ) -> float | None:
        """Cumulative shortfall vs the user's *configured* sleep target over the
        last 7 calendar nights — an estimate, not a physiological measurement.

        Per the coaching model, each night's debt is ``max(0, target - actual)``
        (a long night doesn't "pay back" a short one). The target defaults to
        the user's configured value (7.0h baseline, never a hard-coded 8) and is
        resolved *per night* through the effective-dated history, so recomputing
        an old week after a target change reproduces the original numbers.
        Nights with no sleep data are skipped (watch not worn ≠ no sleep), so
        this is a lower bound. Pass ``target_hours`` to override (e.g. tests)."""
        week = _window(rows, 7)
        nights = [r for r in week if r.get("sleep_hours") is not None]
        if not nights:
            return None
        debt = 0.0
        for r in nights:
            target = (
                target_hours if target_hours is not None
                else self.db.sleep_target_for(r.get("day"))
            )
            debt += max(0.0, target - float(r["sleep_hours"]))
        return round(debt, 1)

    def acute_chronic_ratio(self) -> dict[str, Any]:
        """EWMA acute (7d span) vs chronic (28d span) training load and ratio.

        >1.5 flags a spike (injury/overtraining risk); <0.8 flags detraining.
        EWMA rather than rolling block averages: recent sessions weigh more
        and the decay of fitness/fatigue is modelled, which the injury-risk
        literature finds more sensitive than same-weight windows. Days with
        no data count as zero load — a skipped day is real rest, and dropping
        it would inflate both curves. The ratio is withheld until the history
        spans ``_ACR_MIN_HISTORY_DAYS``.
        """
        rows = self.db.daily_summary(days=_ACR_WARMUP_DAYS)
        if not rows:
            return {"acute_7d": None, "chronic_28d": None, "ratio": None}

        loads = {r["day"]: float(r.get("training_load") or 0) for r in rows}
        start = date.fromisoformat(rows[0]["day"])
        span = (date.today() - start).days + 1
        series = [
            loads.get((start + timedelta(days=i)).isoformat(), 0.0)
            for i in range(span)
        ]
        acute = round(_ewma(series, 7), 2)
        chronic = round(_ewma(series, 28), 2)
        ratio = None
        if chronic > 0 and span >= _ACR_MIN_HISTORY_DAYS:
            ratio = round(acute / chronic, 2)
        return {"acute_7d": acute, "chronic_28d": chronic, "ratio": ratio}

    def flags(self, rows: list[dict[str, Any]]) -> list[str]:
        """Plain-language warnings worth surfacing to the coach and the user."""
        out: list[str] = []

        hrv_streak = self._consecutive_decline(rows, "hrv")
        if hrv_streak >= 3:
            out.append(f"HRV down {hrv_streak} days straight — recovery may be lagging.")

        rhr = self._trend(rows, "resting_hr")
        if rhr["delta"] is not None and rhr["delta"] >= 5:
            out.append(
                f"Resting HR +{rhr['delta']:.0f} bpm vs 28-day baseline — "
                "possible fatigue, illness, or under-recovery."
            )

        debt = self.sleep_debt(rows)
        if debt is not None and debt >= 5:
            out.append(f"Sleep debt of {debt:.1f}h over the last week — prioritise rest.")

        acr = self.acute_chronic_ratio()
        if acr["ratio"] is not None and acr["ratio"] > 1.5:
            out.append(
                f"Training load spiking (acute:chronic {acr['ratio']}) — elevated "
                "injury risk; consider an easier day."
            )
        elif acr["ratio"] is not None and acr["ratio"] < 0.8:
            out.append(
                f"Training load dropping (acute:chronic {acr['ratio']}) — room to "
                "add volume if you feel good."
            )

        steps = self._trend(rows, "steps")
        if steps["avg_7d"] is not None and steps["avg_7d"] < 5000:
            out.append(f"Low activity: averaging {steps['avg_7d']:.0f} steps/day this week.")

        stress = self._trend(rows, "avg_stress")
        if stress["delta"] is not None and stress["delta"] >= 8:
            out.append("Average stress trending up vs baseline.")

        return out

    def report(self, days: int = 28) -> dict[str, Any]:
        """Full structured analysis for the coach prompt / MCP tool."""
        rows = self.db.daily_summary(days=max(days, 28))
        if not rows:
            return {"available": False, "reason": "No data yet — run a pull/backfill."}

        latest = rows[-1]
        return {
            "available": True,
            "as_of": latest.get("day"),
            "latest": latest,
            "trends": {
                "sleep_hours": self._trend(rows, "sleep_hours"),
                "sleep_score": self._trend(rows, "sleep_score"),
                "hrv": self._trend(rows, "hrv"),
                "resting_hr": self._trend(rows, "resting_hr"),
                "steps": self._trend(rows, "steps"),
                "avg_stress": self._trend(rows, "avg_stress"),
                "weight_kg": self._trend(rows, "weight_kg"),
                "body_fat": self._trend(rows, "body_fat"),
            },
            "sleep_debt_7d": self.sleep_debt(rows),
            "sleep_target_hours": self.db.sleep_target_for(),
            "training_load": self.acute_chronic_ratio(),
            "flags": self.flags(rows),
            # Subjective check-in, when the user has logged one — lets the
            # coach weigh how they feel against what the sensors say.
            "readiness": self.db.latest_readiness(),
        }
