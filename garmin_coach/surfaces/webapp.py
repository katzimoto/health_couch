"""Web dashboard — a read-only view of your health data.

A small Starlette app (Starlette ships as a FastMCP dependency, so no extra web
framework) that serves a single-page dashboard plus a JSON API backing it:

    GET  /                      the dashboard page
    GET  /api/report            analyzer report (latest + trends + flags)
    GET  /api/summary?days=N    daily_summary rows
    GET  /api/metric/{name}?days=N   one metric series
    GET  /api/workouts?days=N   recent workouts
    GET  /healthz               liveness

Charts are drawn client-side as inline SVG (no JS dependencies, works offline).

If ``DASHBOARD_TOKEN`` is set, every request must carry it as ``?token=…`` or an
``X-Dashboard-Token`` header — needed before exposing the dashboard publicly. By
default it's unauthenticated and meant for localhost / LAN use.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from garmin_coach.domain.analysis import Analyzer
from garmin_coach.config import settings
from garmin_coach.storage.database import Database
from garmin_coach.storage.models import SUMMARY_COLUMNS

log = logging.getLogger("garmin_coach.web")

_WEB_DIR = Path(__file__).resolve().parent / "web"
_STATIC_DIR = _WEB_DIR / "static"
_INDEX = _WEB_DIR / "templates" / "dashboard.html"

db = Database()
analyzer = Analyzer(db)


def _days(request: Request, default: int = 30, cap: int = 365) -> int:
    try:
        return max(1, min(int(request.query_params.get("days", default)), cap))
    except (TypeError, ValueError):
        return default


# ── Routes ──────────────────────────────────────────────────────────────────────

async def index(_request: Request) -> Response:
    return FileResponse(_INDEX)


async def api_report(_request: Request) -> Response:
    return JSONResponse(analyzer.report())


async def api_summary(request: Request) -> Response:
    return JSONResponse(db.daily_summary(days=_days(request, 30)))


async def api_metric(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in SUMMARY_COLUMNS:
        return JSONResponse({"error": f"unknown metric: {name}"}, status_code=404)
    return JSONResponse(db.metric_series(name, days=_days(request, 60)))


async def api_workouts(request: Request) -> Response:
    return JSONResponse(db.recent_workouts(days=_days(request, 28)))


async def healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


# ── Optional token gate ─────────────────────────────────────────────────────────

class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Require ``DASHBOARD_TOKEN`` (query param or header) when configured."""

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request: Request, call_next):
        # Static assets carry no user data and the <link>/<script> tags that
        # request them can't attach ?token=... themselves, so gating them
        # would leave the page loading with no CSS/JS whenever a token is set.
        if request.url.path == "/healthz" or request.url.path.startswith("/static/"):
            return await call_next(request)
        # Prefer the header where possible: ?token=... ends up in access logs
        # along the way (tunnel, proxies). The query form stays supported
        # because a browser bookmark can't set headers.
        supplied = (
            request.query_params.get("token")
            or request.headers.get("x-dashboard-token")
            or ""
        )
        # compare_digest: constant-time, so response timing can't be used to
        # guess the token byte by byte.
        if not secrets.compare_digest(supplied.encode(), self.token.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def build_app() -> Starlette:
    routes = [
        Route("/", index),
        Route("/api/report", api_report),
        Route("/api/summary", api_summary),
        Route("/api/metric/{name}", api_metric),
        Route("/api/workouts", api_workouts),
        Route("/healthz", healthz),
        Mount("/static", app=StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    ]
    middleware = []
    if settings.dashboard_token:
        middleware.append(
            Middleware(TokenAuthMiddleware, token=settings.dashboard_token)
        )
    else:
        log.warning(
            "DASHBOARD_TOKEN is empty — the dashboard is UNAUTHENTICATED. Keep it "
            "on localhost/LAN, or set a token before exposing it publicly."
        )
    return Starlette(routes=routes, middleware=middleware)


app = build_app()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    log.info(
        "Starting dashboard on %s:%s", settings.dashboard_host, settings.dashboard_port
    )
    uvicorn.run(
        app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
