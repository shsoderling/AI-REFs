"""Shared test configuration."""

import os
import sys
from pathlib import Path

# Qt must never try to open a display in tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make ``src`` importable as a package when pytest is run from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
