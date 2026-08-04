"""
Pytest configuration and path bootstrap.

This file ensures that the project root directory is on ``sys.path``
so that test modules can import the ``app`` package using absolute
imports (e.g. ``from app.main import app``).

Pytest automatically discovers and executes this file before running
any tests, making it the ideal place for path configuration.
"""

import sys
from pathlib import Path

# Resolve the project root (the parent of the directory containing
# this conftest.py file) and add it to ``sys.path`` if not already
# present.  This allows tests to use absolute imports like
# ``from app.main import app`` regardless of the working directory
# from which pytest is invoked.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
