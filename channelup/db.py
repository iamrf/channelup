"""Deduplication store: records which ``(channel, link)`` pairs were already published.

The store is a small protocol so production can use Neon Postgres while tests stay
hermetic with ``MemoryDedupStore``. Keys are scoped per channel, so the same RSS
link can be posted to different channels independently.
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional, Protocol

import psycopg2


def hash_key(channel: str, link: str) -> str:
    """Deterministic dedup key scoped to ``(channel, link)``."""
    return hashlib.sha256(f"{channel}|{link}".encode()).hexdigest()


class DedupStore(Protocol):
    def init(self) -> None: ...

    def is_seen(self, channel: str, link: str) -> bool: ...

    def mark(self, channel: str, link: str, ts: Optional[int] = None) -> None: ...


class MemoryDedupStore:
    """In-memory store for tests and dry runs."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def init(self) -> None:
        pass

    def is_seen(self, channel: str, link: str) -> bool:
        return hash_key(channel, link) in self._seen

    def mark(self, channel: str, link: str, ts: Optional[int] = None) -> None:
        self._seen.add(hash_key(channel, link))


class PostgresDedupStore:
    """Dedup against a PostgreSQL (e.g. Neon) ``published`` table.

    Column usage: ``hash`` is the primary key; ``channel`` records which channel
    the item was posted to (added via idempotent ALTER for existing installs).
    """

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("DATABASE_URL is required")
        self.url = url

    def _connect(self):
        return psycopg2.connect(self.url)

    def init(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS published (hash TEXT PRIMARY KEY, ts INTEGER)")
                cur.execute("ALTER TABLE published ADD COLUMN IF NOT EXISTS channel TEXT")
            conn.commit()

    def is_seen(self, channel: str, link: str) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM published WHERE hash = %s", (hash_key(channel, link),))
                return cur.fetchone() is not None

    def mark(self, channel: str, link: str, ts: Optional[int] = None) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO published (hash, channel, ts) VALUES (%s, %s, %s) "
                    "ON CONFLICT (hash) DO NOTHING",
                    (hash_key(channel, link), channel, ts or int(time.time())),
                )
            conn.commit()