"""FastMCP server exposing read-only health tools to ChatGPT Pro.

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

import logging

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
    """Acute (7d) vs chronic (28d) training load, their ratio, and recent
    workouts. Ratio >1.5 = spike, <0.8 = detraining."""
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


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("Starting MCP server on %s:%s", settings.mcp_host, settings.mcp_port)
    mcp.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
