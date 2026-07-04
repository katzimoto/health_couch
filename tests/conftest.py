"""Shared test setup.

Point DB_PATH at a throwaway location before any garmin_coach module is
imported: mcp_server and webapp construct a module-level Database at import
time, and without this the first import in a test process would create (or
worse, touch) a database at the real default path.
"""

from __future__ import annotations

import os
import tempfile

os.environ.setdefault(
    "DB_PATH", os.path.join(tempfile.mkdtemp(prefix="health-couch-tests-"), "default.db")
)
