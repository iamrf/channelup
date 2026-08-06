"""Runner orchestration: selection, per-channel execution, error isolation."""
import asyncio

from channelup.db import MemoryDedupStore
from channelup.runner import run_channel, run_channels, select_new

from .conftest import make_config


def _items(n, prefix="https://ex.com/"):
    return [{"title": f"t{i}", "link": f"{prefix}{i}", "text": "body", "image": None}
            for i in range(n)]


class FakeBot:
    """Minimal duck-typed bot: records sends, never touches Telegram or network."""

    def __init__(self):
        self.sent_messages = []
        self.sent_photos = []

    async def send_message(self, chat, text, **kwargs):
        self.sent_messages.append((chat, text))

    async def send_photo(self, chat, photo=None, **kwargs):
        self.sent_photos.append((chat,))


def test_select_new_dedups_and_caps():
    store = MemoryDedupStore()
    store.mark("tech", "https://ex.com/1")

    items = _items(10)
    fresh = select_new(store, "tech", items, max_items=3)
    # item "1" already seen, so 0/1 skipped; cap at 3
    assert [i["link"] for i in fresh] == [
        "https://ex.com/0", "https://ex.com/2", "https://ex.com/3"]

    # duplicate links collapse
    dup = _items(5) + _items(5)
    fresh_dup = select_new(store, "tech", dup, max_items=100)
    assert [i["link"] for i in fresh_dup] == [
        "https://ex.com/0", "https://ex.com/2", "https://ex.com/3", "https://ex.com/4"]


def test_select_new_is_per_channel():
    store = MemoryDedupStore()
    store.mark("tech", "https://ex.com/1")
    fresh = select_new(store, "sports", _items(5), max_items=5)
    assert len(fresh) == 5  # sports channel unaffected by tech's history


def test_run_channel_publishes_and_records(monkeypatch):
    """Happy path: item is rewritten, sent, and marked seen."""
    cfg = make_config(rss_sources=["https://nothing.invalid/rss"])

    async def fake_rewrite(session, item, cfg_, prompt):
        assert "{language}" not in prompt
        return f"headline for {item['link']}"

    monkeypatch.setattr("channelup.runner.rewrite", fake_rewrite)

    async def scenario():
        store = MemoryDedupStore()
        bot = FakeBot()
        # override fetch_sources to avoid the network
        monkeypatch.setattr(
            "channelup.runner.fetch_sources", lambda srcs: _items(2))
        report = await run_channel(cfg, cfg.channels[0], store, bot)
        return report, store, bot

    report, store, bot = asyncio.run(scenario())
    assert report.published == 2
    assert report.errors == 0
    assert len(bot.sent_messages) == 2
    # both links recorded as seen
    assert store.is_seen("tech", "https://ex.com/0")
    assert store.is_seen("tech", "https://ex.com/1")

    # second run: nothing new
    async def second():
        report2 = await run_channel(cfg, cfg.channels[0], MemoryDedupStore_with_seen(store), FakeBot())
        return report2
    report2 = asyncio.run(second())
    assert report2.published == 0


def MemoryDedupStore_with_seen(store):
    s = MemoryDedupStore()
    s._seen = store._seen.copy()
    return s