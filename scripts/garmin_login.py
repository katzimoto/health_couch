"""One-time Garmin Connect login — caches auth tokens for the pullers.

Run once (and again roughly every 6 months when tokens expire):

    docker compose run --rm scheduler python scripts/garmin_login.py

Reads GARMIN_EMAIL / GARMIN_PASSWORD from the environment (falls back to
interactive prompts) and handles MFA if your account requires it. Tokens are
written to GARMIN_TOKEN_DIR (default /root/.garminconnect), which is mounted to
./garmin-tokens on the host — so this only needs doing once per machine.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

# Allow running as a plain script (`python scripts/garmin_login.py`).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garminconnect import Garmin  # noqa: E402

from garmin_coach.config import settings  # noqa: E402


def main() -> int:
    email = settings.garmin_email or input("Garmin email: ").strip()
    password = settings.garmin_password or getpass.getpass("Garmin password: ")
    token_dir = settings.garmin_token_dir

    def prompt_mfa() -> str:
        return input("Enter the MFA code Garmin sent you: ").strip()

    print(f"Logging in as {email} …")
    try:
        garmin = Garmin(email=email, password=password, prompt_mfa=prompt_mfa)
        garmin.login()
    except Exception as exc:  # noqa: BLE001
        print(f"❌ Login failed: {exc}", file=sys.stderr)
        return 1

    Path(token_dir).expanduser().mkdir(parents=True, exist_ok=True)
    garmin.garth.dump(token_dir)
    print(f"✅ Logged in. Tokens saved to {token_dir}")
    print("You can now start the stack: docker compose up -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
