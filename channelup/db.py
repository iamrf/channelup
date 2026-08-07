"""Async persistence for ChannelUp.

Writes go to Neon PostgreSQL through asyncpg (a connection pool). ``MemoryStore``
stays hermetic for tests. Two responsibilities:

- **Dedup** — a `(channel, link)` hash in the ``published`` table. ``try_mark_seen``
  is atomic (``INSERT ... ON CONFLICT DO NOTHING``) so the same item is only ever
  produced once, even with concurrent producers.
- **Curate queue** — ``curate_items`` accumulates raw items for ``curate`` feeds
  until the scheduled job claims and processes a batch. Rows are unique per
  ``(channel, link)`` so retried inserts can never double-queue (→ double-post).

Neon is serverless: idle compute can drop a pooled connection mid-query. All
operations run through ``_run`` which retries transient connection errors by
re-acquiring from the pool (asyncpg replaces the broken connection).
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

import asyncpg
from asyncpg import exceptions as apg_exc

MODE_RAW = "raw"
MODE_CUSTOM = "custom_llm"
MODE_CURATE = "curate"

# Errors that mean "the connection died mid-query" (or the pool/connect failed):
# retrying by re-acquiring from the pool is safe and expected to succeed.
_RETRYABLE = (
    apg_exc.PostgresConnectionError,
    apg_exc.ConnectionDoesNotExistError,
    apg_exc.ConnectionFailureError,
    apg_exc.InternalClientError,
    apg_exc.InterfaceError,
    apg_exc.TooManyConnectionsError,
    OSError,
    asyncio.TimeoutError,
)
_RETRY_ATTEMPTS = 4


def hash_key(channel: str, link: str) -> str:
    return hashlib.sha256(f"{channel}|{link}".encode()).hexdigest()


@dataclass
class CurateItem:
    id: int
    channel: str
    feed_url: str
    link: str
    title: str
    text: str
    image: Optional[str] = None

    def to_item(self) -> dict:
        return {"title": self.title, "link": self.link, "text": self.text, "image": self.image}


class Store(Protocol):
    async def init(self) -> None: ...
    async def close(self) -> None: ...
    async def try_mark_seen(self, channel: str, link: str, mode: str) -> bool: ...
    async def enqueue_curate(self, channel: str, feed_url: str, item: dict) -> None: ...
    async def claim_curate(self, channel: str, limit: int) -> list[CurateItem]: ...
    async def mark_curate_processed(self, ids: list[int]) -> None: ...


class MemoryStore:
    """In-memory store for tests / dry runs (async surface, thread-safe via a lock)."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._curate: list[dict] = []
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def init(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def try_mark_seen(self, channel: str, link: str, mode: str = MODE_CUSTOM) -> bool:
        k = hash_key(channel, link)
        async with self._lock:
            if k in self._seen:
                return False
            self._seen.add(k)
            return True

    async def enqueue_curate(self, channel: str, feed_url: str, item: dict) -> None:
        async with self._lock:
            if any(c["channel"] == channel and c["link"] == item["link"] for c in self._curate):
                return  # mirror the DB unique(channel, link) behaviour
            self._curate.append({
                "id": self._next_id,
                "channel": channel,
                "feed_url": feed_url,
                "link": item["link"],
                "title": item.get("title", ""),
                "text": item.get("text", ""),
                "image": item.get("image"),
                "processed": False,
            })
            self._next_id += 1

    async def claim_curate(self, channel: str, limit: int) -> list[CurateItem]:
        async with self._lock:
            rows = [c for c in self._curate
                    if c["channel"] == channel and not c["processed"]][:limit]
            return [
                CurateItem(id=r["id"], channel=r["channel"], feed_url=r["feed_url"],
                           link=r["link"], title=r["title"], text=r["text"], image=r["image"])
                for r in rows
            ]

    async def mark_curate_processed(self, ids: list[int]) -> None:
        idset = set(ids)
        async with self._lock:
            for c in self._curate:
                if c["id"] in idset:
                    c["processed"] = True


class PostgresStore:
    """Neon PostgreSQL backed store (asyncpg pool) with transient-connection retry."""

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("DATABASE_URL is required")
        self.url = url
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=5)
        await self._run_inner("init")

    async def _run_inner(self, _tag: str = ""):
        # init runs schema DDL; retry transient drops as well.
        last = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        "CREATE TABLE IF NOT EXISTS published ("
                        " hash TEXT PRIMARY KEY, channel TEXT NOT NULL, mode TEXT NOT NULL,"
                        " ts BIGINT NOT NULL)"
                    )
                    # Idempotent migration for pre-existing installs.
                    await conn.execute("ALTER TABLE published ADD COLUMN IF NOT EXISTS channel TEXT")
                    await conn.execute("ALTER TABLE published ADD COLUMN IF NOT EXISTS mode TEXT")
                    await conn.execute("ALTER TABLE published ADD COLUMN IF NOT EXISTS ts BIGINT")
                    await conn.execute(
                        "CREATE TABLE IF NOT EXISTS curate_items ("
                        " id BIGSERIAL PRIMARY KEY, channel TEXT NOT NULL, feed_url TEXT NOT NULL,"
                        " link TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, image TEXT,"
                        " created_ts BIGINT NOT NULL, processed_ts BIGINT,"
                        " UNIQUE (channel, link))"
                    )
                    # Migrate a pre-existing curate_items (created before the
                    # UNIQUE constraint existed): collapse any duplicate rows,
                    # then add the unique index that `ON CONFLICT (channel, link)`
                    # in enqueue_curate depends on.
                    await conn.execute(
                        "DELETE FROM curate_items a USING curate_items b "
                        "WHERE a.id > b.id AND a.channel = b.channel AND a.link = b.link"
                    )
                    await conn.execute(
                        "CREATE UNIQUE INDEX IF NOT EXISTS curate_items_channel_link_key "
                        "ON curate_items (channel, link)"
                    )
                return
            except _RETRYABLE as e:
                last = e
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def try_mark_seen(self, channel: str, link: str, mode: str = MODE_CUSTOM) -> bool:
        async def op(conn: asyncpg.Connection) -> bool:
            res = await conn.execute(
                "INSERT INTO published (hash, channel, mode, ts) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (hash) DO NOTHING",
                hash_key(channel, link), channel, mode, int(time.time()),
            )
            return "INSERT 0 1" in res

        return await self._with_retry(op)

    async def enqueue_curate(self, channel: str, feed_url: str, item: dict) -> None:
        async def op(conn: asyncpg.Connection) -> None:
            await conn.execute(
                "INSERT INTO curate_items (channel, feed_url, link, title, text, image, created_ts) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (channel, link) DO NOTHING",
                channel, feed_url, item["link"], item.get("title", ""), item.get("text", ""),
                item.get("image"), int(time.time()),
            )

        await self._with_retry(op)

    async def claim_curate(self, channel: str, limit: int) -> list[CurateItem]:
        async def op(conn: asyncpg.Connection) -> list[CurateItem]:
            rows = await conn.fetch(
                "SELECT id, channel, feed_url, link, title, text, image FROM curate_items "
                "WHERE channel = $1 AND processed_ts IS NULL ORDER BY created_ts, id LIMIT $2",
                channel, limit,
            )
            return [
                CurateItem(id=r["id"], channel=r["channel"], feed_url=r["feed_url"],
                           link=r["link"], title=r["title"], text=r["text"], image=r["image"])
                for r in rows
            ]

        return await self._with_retry(op)

    async def mark_curate_processed(self, ids: list[int]) -> None:
        if not ids:
            return

        async def op(conn: asyncpg.Connection) -> None:
            await conn.execute(
                "UPDATE curate_items SET processed_ts = $1 WHERE id = ANY($2::bigint[])",
                int(time.time()), ids,
            )

        await self._with_retry(op)

    async def _with_retry(self, op: Callable[[asyncpg.Connection], Awaitable]):
        """Run ``op`` against the pool, retrying transient connection errors."""
        last: Optional[BaseException] = None
        for attempt in range(_RETRY_ATTEMPTS):
            try:
                async with self.pool.acquire() as conn:
                    return await op(conn)
            except _RETRYABLE as e:
                last = e
                await asyncio.sleep(0.5 * (attempt + 1))
        assert last is not None
        raise last