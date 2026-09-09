"""Shared pytest configuration.

Qt tests run headless, ``src`` is importable from the repo root, and the
per-model capability memo of the Claude caller is reset between tests so a
fallback memoised by one test never leaks into another.
"""

import os
import sys
from pathlib import Path

# Qt must never try to open a display in tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make ``src`` importable as a package when pytest is run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_claude_caps():
    from src.services.claude_client import reset_caps
    reset_caps()
    yield
    reset_caps()
