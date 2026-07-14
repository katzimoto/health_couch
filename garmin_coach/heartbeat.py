"""Liveness heartbeats for container healthchecks.

Long-running services touch a per-service file under ``<data dir>/heartbeats/``
on a fixed cadence; the compose healthcheck compares the file's mtime against a
staleness threshold. Unlike an ``import``-only healthcheck, this actually fails
when a service's loop is wedged or crashed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from garmin_coach.config import settings

log = logging.getLogger("garmin_coach.heartbeat")


def heartbeat_path(service: str) -> Path:
    return Path(settings.db_path).expanduser().parent / "heartbeats" / service


def beat(service: str) -> None:
    """Touch the service's heartbeat file. Never raises — a full disk or bad
    mount must not take down the service the heartbeat is meant to watch."""
    path = heartbeat_path(service)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        log.warning("Could not write heartbeat file %s", path, exc_info=True)
