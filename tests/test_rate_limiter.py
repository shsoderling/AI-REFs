"""The shared rate limiter and the cache lock hold across threads."""

import threading
import time

from src.services.rate_limiter import RateLimiter
from src.storage.cache_db import CacheDB


def test_rate_limiter_spaces_calls_across_threads():
    limiter = RateLimiter(0.02)
    stamps = []
    lock = threading.Lock()

    def worker():
        for _ in range(5):
            limiter.wait()
            with lock:
                stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(stamps) == 20
    assert time.monotonic() - start >= 19 * 0.02 * 0.9        # 20 calls, 19 gaps
    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
    assert min(gaps) >= 0.02 * 0.5                              # no two calls bunch up


def test_rate_limiter_interval_override():
    limiter = RateLimiter(5.0)
    start = time.monotonic()
    limiter.wait(0)
    limiter.wait(0)
    assert time.monotonic() - start < 0.5


def test_cache_db_concurrent_writes_and_reads():
    cache = CacheDB(":memory:")
    errors = []

    def worker(n):
        try:
            for i in range(50):
                key = f"k{n}-{i}"
                cache.put_article(key, {"n": n, "i": i})
                cache.put_search(key, [n, i])
                assert cache.get_article(key) == {"n": n, "i": i}
                assert cache.get_search(key) == [n, i]
        except Exception as exc:                                # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert cache.get_article("k3-49") == {"n": 3, "i": 49}
