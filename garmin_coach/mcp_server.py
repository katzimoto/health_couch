"""FastMCP server exposing health tools to ChatGPT Pro.

Mostly read tools over the Garmin-derived metrics, plus a few write tools
(``log_meal``, ``log_weight``) for data Garmin doesn't provide — nutrition,
and manual weight entries (e.g. from Apple Health or a scale ChatGPT is told
about in conversation). ``log_meal`` only ever inserts into the separate
Meal table. ``log_weight`` upserts the same per-day Weight row the Garmin
puller writes: whichever source writes a field last wins, but a write never
blanks fields it doesn't carry (upserts are field-preserving).

Runs over streamable HTTP so a public HTTPS URL can reach it. Auth prefers
OAuth via WorkOS AuthKit (``AUTHKIT_DOMAIN`` + ``MCP_PUBLIC_URL``) — ChatGPT's
connector requires OAuth, and AuthKit is a resource-server-only integration:
WorkOS runs the actual authorization server (login, consent, token issuance),
this process only verifies the JWTs it issues. Sign-up is disabled on the
AuthKit environment and exactly one user is provisioned, so only the owner can
ever complete the login step. Falls back to a static bearer token
(``MCP_BEARER_TOKEN``) if AuthKit isn't configured, for simpler local/test use.

Add the connector in ChatGPT: Settings → Connectors → developer mode → your
public URL; ChatGPT discovers the OAuth flow automatically.
"""

from __future__ import annotations

import itertools
import logging
import time
from datetime import date

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.auth.providers.workos import AuthKitProvider

from .analysis import Analyzer
from .config import settings
from .database import Database

log = logging.getLogger("garmin_coach.mcp")

db = Database()
analyzer = Analyzer(db)


def _build_server() -> FastMCP:
    """Construct the FastMCP app, preferring AuthKit OAuth over bearer auth."""
    auth = None
    if settings.authkit_domain and settings.mcp_public_url:
        auth = AuthKitProvider(
            authkit_domain=settings.authkit_domain,
            base_url=settings.mcp_public_url,
        )
    elif settings.mcp_bearer_token:
        auth = StaticTokenVerifier(
            tokens={
                settings.mcp_bearer_token: {
                    "client_id": "chatgpt",
                    "scopes": ["health:read"],
                }
            }
        )
    else:
        log.warning(
            "Neither AUTHKIT_DOMAIN nor MCP_BEARER_TOKEN is set — the server "
            "will be UNAUTHENTICATED. Set one before exposing it publicly."
        )
    return FastMCP(name="Health Coach", auth=auth)


mcp = _build_server()

# Synthetic (negative) activity IDs for manually logged workouts. Time-based so
# they can never collide with Garmin's positive IDs across restarts; the
# counter disambiguates calls that land in the same millisecond.
_synthetic_seq = itertools.count()


def _synthetic_activity_id() -> int:
    return -(int(time.time() * 1000) * 1000 + next(_synthetic_seq) % 1000)


@mcp.tool
def get_daily_summary(days: int = 14) -> list[dict]:
    """Recent daily health summaries (sleep, HRV, resting HR, stress, steps,
    weight, body fat, training load), oldest first. ``days`` caps the window."""
    return db.daily_summary(days=max(1, min(days, 365)))


@mcp.tool
def get_sleep_trend(days: int = 30) -> dict:
    """Sleep hours and sleep score over the last ``days``, with 7d vs 28d
    averages and a sleep-debt figure."""
    report = analyzer.report()
    return {
        "sleep_hours_series": db.metric_series("sleep_hours", days),
        "sleep_score_series": db.metric_series("sleep_score", days),
        "trend": report.get("trends", {}).get("sleep_hours"),
        "sleep_debt_7d": report.get("sleep_debt_7d"),
    }


@mcp.tool
def get_training_load(days: int = 28) -> dict:
    """EWMA-weighted acute (7d) vs chronic (28d) training load, their ratio,
    and recent workouts. Ratio >1.5 = spike, <0.8 = detraining."""
    return {
        "acute_chronic": analyzer.acute_chronic_ratio(),
        "recent_workouts": db.recent_workouts(days=days),
    }


@mcp.tool
def get_body_composition_trend(days: int = 60) -> dict:
    """Weight and body-fat series over ``days`` with 7d vs 28d trend deltas."""
    report = analyzer.report()
    trends = report.get("trends", {})
    return {
        "weight_series": db.metric_series("weight_kg", days),
        "body_fat_series": db.metric_series("body_fat", days),
        "weight_trend": trends.get("weight_kg"),
        "body_fat_trend": trends.get("body_fat"),
    }


@mcp.tool
def get_flags() -> dict:
    """Current recovery/health flags (e.g. HRV decline, resting-HR jump,
    sleep debt, training-load spike) computed from the latest data."""
    report = analyzer.report()
    return {
        "as_of": report.get("as_of"),
        "flags": report.get("flags", []),
        "available": report.get("available", False),
    }


@mcp.tool
def get_full_report() -> dict:
    """Complete analyzer report in one call: latest daily summary, 7d-vs-28d
    trends for every tracked metric (sleep, HRV, resting HR, steps, stress,
    weight, body fat), sleep debt, acute:chronic training load, and flags."""
    return analyzer.report()


@mcp.tool
def get_feedback(days: int = 30) -> list[dict]:
    """Feedback notes logged via the Telegram coach (/done, /skipped, /felt)
    over the last ``days``, oldest first."""
    return db.recent_feedback(days=max(1, min(days, 365)))


@mcp.tool
def get_latest_plan() -> dict | None:
    """The most recently generated morning plan (day + full plan text, plus
    structured details when available), or null if none exists yet."""
    return db.last_plan()


@mcp.tool
def log_meal(
    name: str,
    calories: int | None = None,
    protein_g: float | None = None,
    carbs_g: float | None = None,
    fat_g: float | None = None,
    fiber_g: float | None = None,
    sugar_g: float | None = None,
    day: str | None = None,
    note: str = "",
) -> dict:
    """Log a meal (e.g. from a description, photo, or Apple Health nutrition
    entry the user gives you). Macros are optional. ``day`` defaults to today
    (ISO ``YYYY-MM-DD``). Returns the day's meals so far."""
    target_day = day or date.today().isoformat()
    db.add_meal(
        name=name,
        day=target_day,
        calories=calories,
        protein_g=protein_g,
        carbs_g=carbs_g,
        fat_g=fat_g,
        fiber_g=fiber_g,
        sugar_g=sugar_g,
        note=note or None,
    )
    return {"logged": True, "day": target_day, "meals_today": db.recent_meals(days=1)}


@mcp.tool
def get_meals(days: int = 7) -> list[dict]:
    """Meals logged over the last ``days``, oldest first."""
    return db.recent_meals(days=max(1, min(days, 365)))


@mcp.tool
def log_weight(
    weight_kg: float, day: str | None = None, body_fat: float | None = None
) -> dict:
    """Log a weight reading (e.g. from Apple Health or a scale the user tells
    you about). ``day`` defaults to today. Updates the provided fields on that
    day's entry (a later Garmin sync may update them again); other body-comp
    fields already stored for the day are kept."""
    target_day = day or date.today().isoformat()
    db.upsert_weight(target_day, weight_kg=weight_kg, body_fat=body_fat)
    return {"logged": True, "day": target_day, "weight_kg": weight_kg, "body_fat": body_fat}


@mcp.tool
def log_hydration(intake_ml: int, day: str | None = None, goal_ml: int | None = None) -> dict:
    """Log water/fluid intake for a day (e.g. from Apple Health). ``day``
    defaults to today. Updates the provided fields on that day's entry (a
    later Garmin sync may update them again)."""
    target_day = day or date.today().isoformat()
    db.upsert_hydration(target_day, intake_ml=intake_ml, goal_ml=goal_ml)
    return {"logged": True, "day": target_day, "intake_ml": intake_ml}


@mcp.tool
def log_workout(
    name: str,
    type: str,
    day: str | None = None,
    duration_s: float | None = None,
    distance_m: float | None = None,
    calories: int | None = None,
    avg_hr: int | None = None,
    max_hr: int | None = None,
) -> dict:
    """Log a workout not synced from Garmin (e.g. from Apple Health or a
    manual description). ``day`` defaults to today. Assigned a synthetic
    negative activity ID so it never collides with a real Garmin activity."""
    target_day = day or date.today().isoformat()
    activity_id = _synthetic_activity_id()
    db.upsert_workout(
        activity_id,
        target_day,
        name=name,
        type=type,
        duration_s=duration_s,
        distance_m=distance_m,
        calories=calories,
        avg_hr=avg_hr,
        max_hr=max_hr,
    )
    return {"logged": True, "day": target_day, "activity_id": activity_id}


@mcp.tool
def log_vital(
    metric: str, value: float, unit: str = "", day: str | None = None, note: str = ""
) -> dict:
    """Log any biometric reading not covered by a dedicated tool — e.g. blood
    pressure ("blood_pressure_systolic"/"_diastolic"), "blood_glucose",
    "oxygen_saturation", "respiratory_rate", "height_cm", or any other named
    Apple Health metric the user gives you. ``day`` defaults to today."""
    target_day = day or date.today().isoformat()
    db.add_vital(metric=metric, value=value, day=target_day, unit=unit or None, note=note or None)
    return {"logged": True, "day": target_day, "metric": metric, "value": value}


@mcp.tool
def get_vitals(metric: str | None = None, days: int = 30) -> list[dict]:
    """Vitals logged via log_vital over the last ``days``, oldest first.
    Pass ``metric`` to filter to one named metric, or omit for all."""
    return db.recent_vitals(metric=metric, days=max(1, min(days, 365)))


@mcp.tool
def log_apple_health_export(records: list[dict]) -> dict:
    """Bulk-import structured records read from an Apple Health export (or
    any other source) in one call, instead of calling the single-item log_*
    tools in a loop. Read the export yourself (the user will share it, e.g.
    as a file), extract the values, and pass every record here at once. Each
    record is a dict shaped like one of:

        {"kind": "weight", "day": "2026-07-01", "weight_kg": 78.2, "body_fat": 18.5}
        {"kind": "meal", "day": "2026-07-01", "name": "Breakfast", "calories": 350,
         "protein_g": 20, "carbs_g": 40, "fat_g": 10, "fiber_g": 5, "sugar_g": 8}
        {"kind": "workout", "day": "2026-07-01", "name": "Run", "type": "running",
         "duration_s": 1800, "distance_m": 5000, "calories": 320, "avg_hr": 145}
        {"kind": "hydration", "day": "2026-07-01", "intake_ml": 500}
        {"kind": "vital", "day": "2026-07-01", "metric": "blood_pressure_systolic",
         "value": 120, "unit": "mmHg"}

    ``day`` defaults to today if omitted. Every record is applied
    independently — one bad or malformed record doesn't fail the batch — and
    per-record status is returned so failures are visible.
    """
    results: list[dict] = []
    for i, rec in enumerate(records):
        kind = rec.get("kind")
        day = rec.get("day") or date.today().isoformat()
        try:
            if kind == "weight":
                db.upsert_weight(day, weight_kg=rec["weight_kg"], body_fat=rec.get("body_fat"))
            elif kind == "meal":
                db.add_meal(
                    name=rec["name"],
                    day=day,
                    calories=rec.get("calories"),
                    protein_g=rec.get("protein_g"),
                    carbs_g=rec.get("carbs_g"),
                    fat_g=rec.get("fat_g"),
                    fiber_g=rec.get("fiber_g"),
                    sugar_g=rec.get("sugar_g"),
                    note=rec.get("note"),
                )
            elif kind == "workout":
                db.upsert_workout(
                    _synthetic_activity_id(),
                    day,
                    name=rec.get("name"),
                    type=rec.get("type"),
                    duration_s=rec.get("duration_s"),
                    distance_m=rec.get("distance_m"),
                    calories=rec.get("calories"),
                    avg_hr=rec.get("avg_hr"),
                    max_hr=rec.get("max_hr"),
                )
            elif kind == "hydration":
                db.upsert_hydration(day, intake_ml=rec["intake_ml"], goal_ml=rec.get("goal_ml"))
            elif kind == "vital":
                db.add_vital(
                    metric=rec["metric"],
                    value=rec["value"],
                    day=day,
                    unit=rec.get("unit"),
                    note=rec.get("note"),
                )
            else:
                results.append({"index": i, "status": f"error: unknown kind {kind!r}"})
                continue
            results.append({"index": i, "status": "ok"})
        except Exception as exc:  # noqa: BLE001 — one bad record must not abort the batch
            results.append({"index": i, "status": f"error: {exc}"})
    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {
        "total": len(records),
        "ok": ok_count,
        "failed": len(records) - ok_count,
        "results": results,
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("Starting MCP server on %s:%s", settings.mcp_host, settings.mcp_port)
    mcp.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
