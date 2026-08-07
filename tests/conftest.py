"""Shared builders for the ChannelUp test suite (pytest)."""
import pytest

from channelup.config import Config, ChannelConfig, FeedConfig


def make_feed(url: str = "https://ex.com/rss", interval: int = 300,
              mode: str = "custom_llm", **kw) -> FeedConfig:
    return FeedConfig(url=url, interval=interval, mode=mode, **kw)


def make_channel(name: str = "tech", target: str = "@tech",
                 feeds: tuple | list | None = None, **kw) -> ChannelConfig:
    if feeds is None:
        feeds = (make_feed(),)
    data = dict(
        name=name, telegram_target=target, feeds=tuple(feeds),
        language="en", rate_per_minute=19,
        curate_interval_seconds=600, curate_batch_size=10, curate_top_n=5,
    )
    data.update(kw)
    return ChannelConfig(**data)


def make_config(channels: tuple | list | None = None, **kw) -> Config:
    if channels is None:
        channels = (make_channel(),)
    data = dict(
        bot_token="t",
        admin_user_ids=frozenset({1, 2}),
        database_url="postgresql://x",
        llm_provider="deepseek",
        llm_api_key="k",
        llm_model="m",
        llm_base_url="",
        llm_concurrency=2,
        llm_rate_per_minute=60,
        telegram_rate_per_minute=19,
        queue_sizes=(64, 64),
        channels=tuple(channels),
    )
    data.update(kw)
    return Config(**data)


@pytest.fixture
def env():
    return {
        "TELEGRAM_BOT_TOKEN": "123:secret",
        "DATABASE_URL": "postgresql://user:pass@neon/db",
        "LLM_API_KEY": "sk-abc",
        "ADMIN_USER_IDS": "1,2,3",
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": "gpt-4o-mini",
    }