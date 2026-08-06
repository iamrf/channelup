"""Shared fixtures for the ChannelUp test suite (pytest)."""
import pytest

from channelup.config import Config, ChannelConfig


def make_config(**channel_attrs) -> Config:
    """A minimal Config for unit tests (single channel)."""
    channel = ChannelConfig(
        name=channel_attrs.get("name", "tech"),
        telegram_target=channel_attrs.get("telegram_target", "@tech"),
        rss_sources=tuple(channel_attrs.get("rss_sources", ["https://ex.com/rss"])),
        prompt_addon=channel_attrs.get("prompt_addon", ""),
        language=channel_attrs.get("language", "en"),
        max_items_per_run=channel_attrs.get("max_items_per_run", 5),
        post_delay_seconds=channel_attrs.get("post_delay_seconds", 0.0),
    )
    return Config(
        bot_token="t",
        admin_user_ids=frozenset({1}),
        database_url="postgresql://x",
        llm_provider="deepseek",
        llm_api_key="k",
        llm_model="m",
        llm_base_url="",
        publish_interval_minutes=30,
        channels=(channel,),
    )


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