"""Producer-Consumer pipeline: raw / custom_llm / curate routing + dedup."""
import asyncio

import pytest

from channelup.config import FeedConfig
from channelup.db import MemoryStore
from channelup.pipeline import MODE_CURATE, MODE_CUSTOM, MODE_RAW, Pipeline

from .conftest import make_channel, make_config, make_feed


class FakeBot:
    def __init__(self):
        self.messages = []   # (chat, text)
        self.photos = []

    async def send_message(self, chat, text, **kwargs):
        self.messages.append((chat, text))

    async def send_photo(self, chat, photo=None, **kwargs):
        self.photos.append((chat,))


def _items(n, base="https://ex.com/"):
    return [{"title": f"t{i}", "link": f"{base}{i}", "text": f"body {i}", "image": None}
            for i in range(n)]


def run(coro):
    return asyncio.run(coro)


def _sweep(monkeypatch, cfg, fetch_items, rewrite=None, select_top=None):
    """Build config + store + pipeline, stub fetch/LLM, run a sweep, return pipeline."""
    store = MemoryStore()

    async def scenario():
        await store.init()
        bot = FakeBot()
        pipe = Pipeline(cfg, store, bot)
        try:
            monkeypatch.setattr("channelup.pipeline.fetch_sources",
                                lambda feeds: fetch_items)
            if rewrite is not None:
                monkeypatch.setattr("channelup.pipeline.rewrite", rewrite)
            if select_top is not None:
                monkeypatch.setattr("channelup.pipeline.select_top", select_top)
            published = await pipe.sweep()
            return published, pipe, bot, store
        finally:
            await pipe.stop()
            await store.close()

    return run(scenario())


def test_raw_mode_bypasses_llm(monkeypatch):
    channel = make_channel(feeds=(make_feed(mode="raw", target_link="https://t.me/x"),))
    cfg = make_config(channels=(channel,))
    script = []

    async def bogus_rewrite(*a, **k):
        script.append("rewrite-called")
        return "SHOULD NOT HAPPEN"

    published, pipe, bot, store = _sweep(
        monkeypatch, cfg, fetch_items=_items(2), rewrite=bogus_rewrite)
    assert script == [], "raw mode must never call the LLM"
    assert published == 2
    assert len(bot.messages) == 2
    # raw posts carry the target_link and no source suffix duplication
    assert "https://t.me/x" in bot.messages[0][1]


def test_custom_llm_routes_through_llm_and_publishes(monkeypatch):
    channel = make_channel(feeds=(make_feed(mode="custom_llm"),))
    cfg = make_config(channels=(channel,))
    calls = []

    async def rewrite(session, item, cfg_, system):
        calls.append(system)
        return f"rewritten {item['link']}"

    published, pipe, bot, store = _sweep(monkeypatch, cfg, fetch_items=_items(2),
                                         rewrite=rewrite)
    assert len(calls) == 2           # one LLM rewrite per item
    assert published == 2
    assert bot.messages[0][1].startswith("rewritten https://ex.com/0")
    assert pipe.stats["rewritten"] == 2


def test_custom_prompt_used_for_item(monkeypatch):
    channel = make_channel(feeds=(make_feed(mode="custom_llm",
                                            custom_prompt="Do X in {language}."),))
    cfg = make_config(channels=(channel,))
    seen = []

    async def rewrite(session, item, cfg_, system):
        seen.append(system)
        return "ok"

    _sweep(monkeypatch, cfg, fetch_items=_items(1), rewrite=rewrite)
    assert seen == ["Do X in en."]


def test_curate_accumulates_then_publishes_top(monkeypatch):
    channel = make_channel(feeds=(make_feed(mode="curate"),),
                           curate_batch_size=10, curate_top_n=2)
    cfg = make_config(channels=(channel,))

    async def select_top(session, items, cfg_, language, top_n):
        # LLM "picks" the last `top_n` candidates.
        return list(reversed(items))[:top_n]

    async def rewrite(session, item, cfg_, system):
        return f"curated {item['link']}"

    published, pipe, bot, store = _sweep(
        monkeypatch, cfg, fetch_items=_items(4), rewrite=rewrite, select_top=select_top)
    assert published == 2
    assert len(bot.messages) == 2
    assert pipe.stats["curated"] == 2
    # nothing left to claim after processing
    assert run(store.claim_curate("tech", 10)) == []


def test_dedup_prevents_duplicate_production(monkeypatch):
    channel = make_channel(feeds=(make_feed(mode="raw"),))
    cfg = make_config(channels=(channel,))

    async def run_twice():
        store = MemoryStore()
        await store.init()
        bot = FakeBot()
        pipe = Pipeline(cfg, store, bot)
        monkeypatch.setattr("channelup.pipeline.fetch_sources",
                            lambda feeds: _items(2))
        try:
            await pipe.sweep()
            first = pipe.stats["published"]
            await pipe.sweep()          # same feed, same items -> already seen
            second = pipe.stats["published"]
            return first, second, len(bot.messages)
        finally:
            await pipe.stop()
            await store.close()

    first, second, msgs = run(run_twice())
    assert first == 2 and second == 2      # second sweep publishes nothing new
    assert msgs == 2


def test_channel_without_curate_skips_curate(monkeypatch):
    channel = make_channel(feeds=(make_feed(mode="custom_llm"),))
    cfg = make_config(channels=(channel,))
    hits = []

    async def select_top(*a, **k):
        hits.append("select-top")
        return []

    async def rewrite(session, item, cfg_, system):
        return "ok"

    _sweep(monkeypatch, cfg, fetch_items=_items(1), rewrite=rewrite, select_top=select_top)
    assert hits == [], "select_top must not run for a channel without curate feeds"


def test_build_raw_text_uses_target_link():
    async def scenario():
        channel = make_channel(feeds=(make_feed(mode="raw", target_link="https://t.me/chan"),))
        store = MemoryStore()
        pipe = Pipeline(make_config(), store, FakeBot())
        text = pipe._build_raw_text(channel.feeds[0],
                                    {"title": "A", "text": "Body", "link": "https://src/x",
                                     "image": None})
        await pipe.stop()
        return text
    text = run(scenario())
    assert "A" in text and "Body" in text
    assert "https://t.me/chan" in text
    assert "https://src/x" not in text