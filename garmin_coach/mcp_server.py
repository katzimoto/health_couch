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

**Structure:** the 72 tools live in thematic modules under
:mod:`garmin_coach.mcp_tools` (metrics, coaching, nutrition, sync, profile,
strength, training, workouts, reminders); each exposes a ``register(mcp)``.
This module only builds the authenticated server and wires those modules onto
it. The tool functions are re-exported here (``from ...mcp_tools.X import *``)
so ``mcp_server.<tool>`` stays importable/callable for tests. Shared, lazily
constructed handles (the ``Database``, Garmin client, Telegram sender) live in
:mod:`garmin_coach.mcp_tools.runtime` — importing this module no longer opens a
database connection.
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.auth.providers.workos import AuthKitProvider

from .config import settings
from .mcp_tools import (
    coaching,
    metrics,
    nutrition,
    profile,
    reminders,
    runtime,
    strength,
    sync,
    training,
    workouts,
)

# Re-export every tool at module level so ``mcp_server.<tool>`` resolves — the
# MCP client sees them via the server registration below; tests call them here.
from .mcp_tools.coaching import *  # noqa: F401,F403
from .mcp_tools.metrics import *  # noqa: F401,F403
from .mcp_tools.nutrition import *  # noqa: F401,F403
from .mcp_tools.profile import *  # noqa: F401,F403
from .mcp_tools.reminders import *  # noqa: F401,F403
from .mcp_tools.strength import *  # noqa: F401,F403
from .mcp_tools.sync import *  # noqa: F401,F403
from .mcp_tools.training import *  # noqa: F401,F403
from .mcp_tools.workouts import *  # noqa: F401,F403

log = logging.getLogger("garmin_coach.mcp")

# Reset the runtime's cached handles on (re)import so an ``importlib.reload`` of
# this module — the pattern the migration tests use to rebind onto a temp DB —
# forces the lazy ``get_db`` to rebuild against the current settings.
runtime.set_db(None)

_TOOL_MODULES = (
    metrics, coaching, nutrition, sync, profile, strength, training, workouts, reminders,
)


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
    server = FastMCP(name="Health Coach", auth=auth)
    for module in _TOOL_MODULES:
        module.register(server)
    return server


mcp = _build_server()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info("Starting MCP server on %s:%s", settings.mcp_host, settings.mcp_port)
    mcp.run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
