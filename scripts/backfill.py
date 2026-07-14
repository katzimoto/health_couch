"""Import historical Garmin data on demand.

    docker compose run --rm scheduler python scripts/backfill.py [DAYS]

Defaults to BACKFILL_DAYS (90). Idempotent — re-running just refreshes the same
days, so it's safe to run again if a pull was interrupted.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_coach.config import settings  # noqa: E402
from garmin_coach.ingest.garmin_client import GarminClient  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    days = settings.backfill_days
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            print(f"Invalid DAYS argument: {sys.argv[1]!r}", file=sys.stderr)
            return 1

    client = GarminClient()
    try:
        client.login()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Garmin login failed: {exc}", file=sys.stderr)
        print("Run scripts/garmin_login.py first to cache tokens.", file=sys.stderr)
        return 1

    client.backfill(days=days)
    print(f"✅ Backfilled {days} days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
