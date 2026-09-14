"""SQLite cache for PubMed and bioRxiv queries and article metadata."""

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, Any

from ..services.rate_limiter import synchronized

logger = logging.getLogger(__name__)

# Default cache location — survives app restarts
_DEFAULT_CACHE_DIR = Path.home() / ".ai_refs"

# Cache entries older than this (in seconds) are ignored on read
_DEFAULT_TTL = 60 * 60 * 24 * 30  # 30 days


class CacheDB:
    """Lightweight persistent cache backed by SQLite.

    Parameters
    ----------
    db_path : str or Path
        Path to the SQLite database file. If empty, uses the default
        ``~/.ai_refs/cache.db``.  Pass ``":memory:"`` for a purely
        in-memory cache (useful for testing).
    ttl : int
        Time-to-live for cache entries in seconds (default 30 days).
    """

    def __init__(self, db_path: str = "", ttl: int = _DEFAULT_TTL):
        if not db_path:
            _DEFAULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            db_path = str(_DEFAULT_CACHE_DIR / "cache.db")

        self._ttl = ttl
        # One connection shared by the search worker threads; every public
        # method runs under this lock.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()
        logger.debug(f"CacheDB initialized ({db_path})")

    def _create_tables(self):
        # Migrate from old schema if needed — the old version used
        # different column names (query_hash/pmid instead of key, etc.)
        self._migrate_if_needed()
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS search_cache (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                ts    REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS article_cache (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                ts    REAL NOT NULL
            );
        """)

    def _migrate_if_needed(self):
        """Drop old-schema tables so they can be recreated with the new schema."""
        for table in ("search_cache", "article_cache"):
            try:
                cols = {
                    row[1]
                    for row in self._conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
            except Exception:
                cols = set()
            if cols and "key" not in cols:
                logger.info(f"Migrating {table}: dropping old schema")
                self._conn.execute(f"DROP TABLE {table}")
                self._conn.commit()

    # ── Search cache ────────────────────────────────────────────────

    @synchronized
    def get_search(self, key: str) -> Optional[Any]:
        row = self._conn.execute(
            "SELECT value, ts FROM search_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if time.time() - row[1] > self._ttl:
            self._conn.execute("DELETE FROM search_cache WHERE key = ?", (key,))
            self._conn.commit()
            return None
        return json.loads(row[0])

    @synchronized
    def put_search(self, key: str, value: Any):
        self._conn.execute(
            "INSERT OR REPLACE INTO search_cache (key, value, ts) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        self._conn.commit()

    # ── Article cache ───────────────────────────────────────────────

    @synchronized
    def get_article(self, key: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT value, ts FROM article_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if time.time() - row[1] > self._ttl:
            self._conn.execute("DELETE FROM article_cache WHERE key = ?", (key,))
            self._conn.commit()
            return None
        return json.loads(row[0])

    @synchronized
    def put_article(self, key: str, value: dict):
        self._conn.execute(
            "INSERT OR REPLACE INTO article_cache (key, value, ts) VALUES (?, ?, ?)",
            (key, json.dumps(value), time.time()),
        )
        self._conn.commit()

    # ── Housekeeping ────────────────────────────────────────────────

    @synchronized
    def clear(self):
        """Remove all cached entries."""
        self._conn.execute("DELETE FROM search_cache")
        self._conn.execute("DELETE FROM article_cache")
        self._conn.commit()

    @synchronized
    def prune_expired(self):
        """Remove entries older than the TTL."""
        cutoff = time.time() - self._ttl
        self._conn.execute("DELETE FROM search_cache WHERE ts < ?", (cutoff,))
        self._conn.execute("DELETE FROM article_cache WHERE ts < ?", (cutoff,))
        self._conn.commit()

    @synchronized
    def close(self):
        """Close the database connection."""
        self._conn.close()
