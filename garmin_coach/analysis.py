"""Trend analyzer — runs before any LLM call.

Turns raw daily rows into the comparisons a coach actually reasons about: 7-day
vs 28-day baselines, sleep debt, an acute:chronic training-load ratio, and a set
of plain-language flags ("HRV down 3 days straight"). The output is a compact
dict that gets injected into the coach prompt, so the LLM spends its budget on
advice rather than arithmetic.
"""

from __future__ import annotations

from statistics import mean
from typing import Any

from .database import Database


def _avg(values: list[float | int | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    return round(mean(clean), 2) if clean else None


def _delta(recent: float | None, baseline: float | None) -> float | None:
    if recent is None or baseline is None:
        return None
    return round(recent - baseline, 2)


def _series(rows: list[dict[str, Any]], key: str) -> list[float | None]:
    return [r.get(key) for r in rows]


class Analyzer:
    def __init__(self, db: Database | None = None) -> None:
        self.db = db or Database()

    def _trend(self, rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
        """7-day vs 28-day average and their delta for a single metric."""
        last7 = _avg(_series(rows[-7:], key))
        last28 = _avg(_series(rows[-28:], key))
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

    def sleep_debt(self, rows: list[dict[str, Any]], target_hours: float = 8.0) -> float | None:
        """Cumulative shortfall vs ``target_hours`` over the last 7 nights."""
        hours = [r.get("sleep_hours") for r in rows[-7:] if r.get("sleep_hours") is not None]
        if not hours:
            return None
        return round(sum(target_hours - h for h in hours), 1)

    def acute_chronic_ratio(self) -> dict[str, Any]:
        """Acute (7d) vs chronic (28d) daily training load, and their ratio.

        >1.5 flags a spike (injury/overtraining risk); <0.8 flags detraining.
        """
        rows = self.db.daily_summary(days=28)
        acute = _avg(_series(rows[-7:], "training_load"))
        chronic = _avg(_series(rows[-28:], "training_load"))
        ratio = round(acute / chronic, 2) if acute and chronic else None
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
            "training_load": self.acute_chronic_ratio(),
            "flags": self.flags(rows),
        }
