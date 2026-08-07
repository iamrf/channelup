"""Async persistence for ChannelUp.

Writes go to Neon PostgreSQL through asyncpg (a connection pool). The ``Store``
protocol keeps tests hermetic with ``MemoryStore``. Two responsibilities:

- **Dedup** — a `(channel, link)` hash in the ``published`` table. ``try_mark_seen``
  is atomic (``INSERT ... ON CONFLICT DO NOTHING``) so the same item is only ever
  produced once, even with concurrent producers.
- **Curate queue** — ``curate_items`` accumulates raw items for ``curate`` feeds
  until the scheduled job claims and processes a batch.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Optional, Protocol

import asyncpg

MODE_RAW = "raw"
MODE_CUSTOM = "custom_llm"
MODE_CURATE = "curate"


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
    """Neon PostgreSQL backed store (asyncpg pool)."""

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("DATABASE_URL is required")
        self.url = url
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self) -> None:
        self.pool = await asyncpg.create_pool(self.url, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS published ("
                " hash TEXT PRIMARY KEY, channel TEXT NOT NULL, mode TEXT NOT NULL,"
                " ts BIGINT NOT NULL)"
            )
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS curate_items ("
                " id BIGSERIAL PRIMARY KEY, channel TEXT NOT NULL, feed_url TEXT NOT NULL,"
                " link TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL, image TEXT,"
                " created_ts BIGINT NOT NULL, processed_ts BIGINT)"
            )

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def try_mark_seen(self, channel: str, link: str, mode: str = MODE_CUSTOM) -> bool:
        async with self.pool.acquire() as conn:
            res = await conn.execute(
                "INSERT INTO published (hash, channel, mode, ts) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (hash) DO NOTHING",
                hash_key(channel, link), channel, mode, int(time.time()),
            )
            return "INSERT 0 1" in res

    async def enqueue_curate(self, channel: str, feed_url: str, item: dict) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO curate_items (channel, feed_url, link, title, text, image, created_ts) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                channel, feed_url, item["link"], item.get("title", ""), item.get("text", ""),
                item.get("image"), int(time.time()),
            )

    async def claim_curate(self, channel: str, limit: int) -> list[CurateItem]:
        async with self.pool.acquire() as conn:
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

    async def mark_curate_processed(self, ids: list[int]) -> None:
        if not ids:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE curate_items SET processed_ts = $1 WHERE id = ANY($2::bigint[])",
                int(time.time()), ids,
            )