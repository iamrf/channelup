"""Dedup + curate queue: MemoryStore async surface, per-channel scoping, hashing."""
import asyncio

import pytest

from channelup.db import MemoryStore, PostgresStore, hash_key


def run(coro):
    return asyncio.run(coro)


def test_memory_dedup_per_channel():
    async def scenario():
        s = MemoryStore()
        await s.init()
        assert await s.try_mark_seen("tech", "https://a/1", "raw") is True
        assert await s.try_mark_seen("tech", "https://a/1", "raw") is False   # dup
        assert await s.try_mark_seen("sports", "https://a/1", "raw") is True  # other channel
        await s.close()
    run(scenario())


def test_hash_is_deterministic_and_channel_scoped():
    assert hash_key("tech", "a") == hash_key("tech", "a")
    assert hash_key("tech", "a") != hash_key("sports", "a")
    assert len(hash_key("tech", "a")) == 64


def test_curate_queue_claim_and_process():
    async def scenario():
        s = MemoryStore()
        await s.init()
        for i in range(4):
            await s.enqueue_curate("tech", "https://feed", {
                "link": f"https://a/{i}", "title": f"t{i}", "text": "body", "image": None,
            })
        batch = await s.claim_curate("tech", 3)
        assert [b.link for b in batch] == ["https://a/0", "https://a/1", "https://a/2"]
        assert all(b.image is None for b in batch)
        # process the first batch
        await s.mark_curate_processed([b.id for b in batch])
        remaining = await s.claim_curate("tech", 10)
        assert [b.link for b in remaining] == ["https://a/3"]
        await s.close()
    run(scenario())


def test_curate_claim_respects_channel():
    async def scenario():
        s = MemoryStore()
        await s.init()
        await s.enqueue_curate("tech", "f", {"link": "a", "title": "t", "text": "b"})
        await s.enqueue_curate("sports", "f", {"link": "b", "title": "t", "text": "b"})
        assert len(await s.claim_curate("tech", 10)) == 1
        assert len(await s.claim_curate("sports", 10)) == 1
        await s.close()
    run(scenario())


def test_postgres_store_requires_url():
    with pytest.raises(ValueError):
        PostgresStore("")


def test_postgres_store_exposes_asyncpg_pool_attribute():
    s = PostgresStore("postgresql://x")
    assert s.pool is None  # created on init against a live server only