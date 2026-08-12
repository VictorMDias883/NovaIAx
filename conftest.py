"""
Pytest configuration and path bootstrap.

This file ensures that the project root directory is on ``sys.path``
so that test modules can import the ``app`` package using absolute
imports (e.g. ``from app.main import app``).

It also defaults ``DATABASE_URL`` to a local SQLite file when the
variable is not set explicitly, so the full middleware/router stack can
be exercised in integration tests without requiring PostgreSQL.  This
assignment happens at conftest import time — before any test module is
imported — so the engine created by :mod:`app.db.session` picks it up.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Integration tests (e.g. ``tests/test_auth_and_rate_limit.py``) import
# ``app.main`` directly; give them a working database by default.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
