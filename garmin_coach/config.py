"""Central configuration, loaded once from the environment (`.env`).

Every other module imports :data:`settings` rather than reading ``os.environ``
directly, so there is a single, typed source of truth.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load a local .env if present. In Docker the values arrive via `env_file`, so a
# missing .env is not an error.
load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_int(name: str, default: int) -> int:
    raw = _get(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    """Immutable, validated view of the runtime configuration."""

    # Garmin (only needed for the one-time login script)
    garmin_email: str = field(default_factory=lambda: _get("GARMIN_EMAIL"))
    garmin_password: str = field(default_factory=lambda: _get("GARMIN_PASSWORD"))
    garmin_token_dir: str = field(
        default_factory=lambda: _get("GARMIN_TOKEN_DIR", "/root/.garminconnect")
    )

    # OpenAI
    openai_api_key: str = field(default_factory=lambda: _get("OPENAI_API_KEY"))
    openai_model: str = field(
        default_factory=lambda: _get("OPENAI_MODEL", "gpt-4o-mini")
    )

    # Telegram
    telegram_bot_token: str = field(
        default_factory=lambda: _get("TELEGRAM_BOT_TOKEN")
    )
    telegram_chat_id: str = field(default_factory=lambda: _get("TELEGRAM_CHAT_ID"))

    # MCP server
    mcp_bearer_token: str = field(default_factory=lambda: _get("MCP_BEARER_TOKEN"))
    mcp_host: str = field(default_factory=lambda: _get("MCP_HOST", "0.0.0.0"))
    mcp_port: int = field(default_factory=lambda: _get_int("MCP_PORT", 8000))

    # Web dashboard
    dashboard_host: str = field(
        default_factory=lambda: _get("DASHBOARD_HOST", "0.0.0.0")
    )
    dashboard_port: int = field(
        default_factory=lambda: _get_int("DASHBOARD_PORT", 8050)
    )
    # Optional shared secret; if set, the dashboard requires ?token=... (or an
    # X-Dashboard-Token header) so it can be exposed publicly behind the tunnel.
    dashboard_token: str = field(
        default_factory=lambda: _get("DASHBOARD_TOKEN")
    )

    # Coaching
    coach_goals: str = field(
        default_factory=lambda: _get(
            "COACH_GOALS",
            "Lose fat, improve energy, and build a sustainable routine.",
        )
    )
    morning_plan_time: str = field(
        default_factory=lambda: _get("MORNING_PLAN_TIME", "07:30")
    )
    timezone: str = field(default_factory=lambda: _get("TZ", "UTC"))

    # Storage
    db_path: str = field(
        default_factory=lambda: _get("DB_PATH", "/app/data/health.db")
    )
    backfill_days: int = field(
        default_factory=lambda: _get_int("BACKFILL_DAYS", 90)
    )

    def morning_plan_hm(self) -> tuple[int, int]:
        """Return the morning-plan time as an ``(hour, minute)`` tuple."""
        try:
            hh, mm = self.morning_plan_time.split(":", 1)
            return int(hh), int(mm)
        except (ValueError, AttributeError):
            return 7, 30

    def ensure_db_parent(self) -> None:
        """Create the directory that will hold the SQLite file, if needed."""
        Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
