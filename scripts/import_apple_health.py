"""Import an Apple Health export (export.zip or export.xml) into the database.

    # 1. Copy the export into ./data on the host so the container can see it:
    #    scp export.zip <vps>:~/health_couch/data/
    # 2. Run the import inside any service container:
    docker compose run --rm scheduler python scripts/import_apple_health.py /app/data/export.zip

Options:
    --since YYYY-MM-DD   only import records on/after this date

Imports weight/body-comp, hydration, nutrition (one daily-total meal), workouts
and vitals. Steps/sleep/HR are NOT imported — Garmin owns those. Idempotent:
re-running replaces this importer's own rows and never duplicates.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_coach.ingest.apple_health import import_export  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Path to export.zip or export.xml")
    parser.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                        help="Only import records on/after this date")
    args = parser.parse_args()

    if not Path(args.path).expanduser().exists():
        print(f"❌ File not found: {args.path}", file=sys.stderr)
        return 1

    counts = import_export(args.path, since=args.since)
    print("✅ Apple Health import complete:")
    for key, value in counts.items():
        print(f"   {key.replace('_', ' ')}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
