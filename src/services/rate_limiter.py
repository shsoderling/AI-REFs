"""Thread-safe rate limiting and a small method-locking helper.

The literature clients are shared by the sentence-search worker threads,
so one limiter per client keeps the whole process under the provider's
request rate (NCBI: 3/s without a key, 10/s with one).
"""

from __future__ import annotations

import functools
import threading
import time
from typing import Optional


class RateLimiter:
    """Block so that consecutive ``wait()`` calls are at least ``min_interval`` apart,
    across threads."""

    def __init__(self, min_interval: float):
        self.min_interval = float(min_interval)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self, interval: Optional[float] = None) -> None:
        gap = self.min_interval if interval is None else float(interval)
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + gap
        delay = start - now
        if delay > 0:
            time.sleep(delay)


def synchronized(method):
    """Run *method* under ``self._lock`` (an ``RLock`` so methods may nest)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper
