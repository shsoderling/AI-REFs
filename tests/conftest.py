"""Shared pytest configuration.

Qt tests run headless, and the per-model capability memo of the Claude
caller is reset between tests so a fallback memoised by one test never
leaks into another.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_claude_caps():
    try:
        from src.services.claude_client import reset_caps
    except ImportError:  # module lands in a later step
        yield
        return
    reset_caps()
    yield
    reset_caps()
