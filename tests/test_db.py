"""Dedup store: in-memory store correctness and per-channel scoping."""
from channelup.db import MemoryDedupStore, PostgresDedupStore, hash_key


def test_in_memory_store_roundtrip():
    store = MemoryDedupStore()
    store.init()
    assert store.is_seen("news", "https://ex.com/a") is False
    store.mark("news", "https://ex.com/a")
    assert store.is_seen("news", "https://ex.com/a") is True
    store.mark("news", "https://ex.com/a")  # idempotent
    assert store.is_seen("news", "https://ex.com/a") is True
    assert store.is_seen("news", "https://ex.com/b") is False


def test_keys_are_scoped_per_channel():
    store = MemoryDedupStore()
    store.mark("news", "https://ex.com/a")
    # Same link, different channel -> not seen
    assert store.is_seen("sports", "https://ex.com/a") is False


def test_hash_is_deterministic_and_channel_scoped():
    assert hash_key("news", "https://ex.com/a") == hash_key("news", "https://ex.com/a")
    assert hash_key("news", "https://ex.com/a") != hash_key("sports", "https://ex.com/a")
    assert len(hash_key("news", "https://ex.com/a")) == 64  # hex sha256


def test_postgres_store_requires_url():
    try:
        PostgresDedupStore("")
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_postgres_store_mark_inserts_channel(monkeypatch):
    """Verify the DDL/SQL issued by the Live store without a real database."""
    store = PostgresDedupStore("postgresql://x")
    captured = []

    class FakeCursor:
        def __init__(self, data=None):
            self.data = data or []

        def execute(self, sql, params=None):
            captured.append((sql.replace("\n", " ").strip(), params))

        def fetchone(self):
            return self.data.pop() if self.data else None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeConn:
        def __init__(self, cursor=None):
            self._cursor = cursor or FakeCursor()
            self.closed = False
            self.committed = 0

        def cursor(self):
            return self._cursor

        def commit(self):
            self.committed += 1

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(store, "_connect", lambda: FakeConn())
    store.init()
    store.mark("news", "https://ex.com/a")

    sqls = [c[0] for c in captured]
    assert any("CREATE TABLE IF NOT EXISTS published" in s for s in sqls)
    assert any("ADD COLUMN IF NOT EXISTS channel" in s for s in sqls)
    # the INSERT carries the hash + channel name
    insert = [c for c in captured if c[0].startswith("INSERT")][0]
    assert insert[1][1] == "news"
    assert insert[1][0] == hash_key("news", "https://ex.com/a")