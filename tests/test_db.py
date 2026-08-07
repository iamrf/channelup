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


def test_postgres_store_init_migrates_existing_schema(monkeypatch):
    """init() must add missing columns to a pre-existing `published` table."""
    import asyncpg

    executed = []

    class FakeConn:
        async def execute(self, sql, *args):
            executed.append(sql.replace("\n", " ").strip())
            return "CREATE TABLE"

    class FakeClearAcquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *a):
            return False

    class FakePool:
        def __init__(self):
            self.closed = False
            self.conn = FakeConn()

        def acquire(self):
            return FakeClearAcquire(self.conn)

        async def close(self):
            self.closed = True

    pool = FakePool()

    async def fake_create_pool(*a, **k):
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)

    async def scenario():
        s = PostgresStore("postgresql://x")
        await s.init()
        await s.close()
        return s

    run(scenario())
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS published") for s in executed)
    for col in (("channel",), ("mode",), ("ts",)):
        join = " ".join(col)
        assert any(f"ALTER TABLE published ADD COLUMN IF NOT EXISTS {join}"
                   in s for s in executed), f"missing ALTER for {join}"
    assert any(s.startswith("CREATE TABLE IF NOT EXISTS curate_items") for s in executed)
    # fresh curate table carries a unique(channel, link) so retried enqueues can't
    # double-queue an item (prevents double-post).
    assert any("UNIQUE (channel, link)" in s for s in executed)


def test_postgres_store_retries_transient_connection_errors():
    """_with_retry re-acquires from the pool after a mid-query connection drop."""
    from asyncpg import exceptions as apg_exc

    class Conn:
        def __init__(self):
            self.calls = 0

        async def doit(self):
            self.calls += 1
            if self.calls == 1:
                raise apg_exc.ConnectionDoesNotExistError("connection was closed in the middle")
            return "ok"

    class FakeAcquire:
        def __init__(self, conn):
            self.conn = conn

        async def __aenter__(self):
            return self.conn

        async def __aexit__(self, *a):
            return False

    class FakePool:
        def __init__(self, conn):
            self.conn = conn

        def acquire(self):
            return FakeAcquire(self.conn)

    store = PostgresStore("postgresql://x")
    conn = Conn()
    store.pool = FakePool(conn)

    async def op(c):
        return await c.doit()

    assert run(store._with_retry(op)) == "ok"
    assert conn.calls == 2, "operation retried after the transient drop"