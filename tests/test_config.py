"""Config parsing, per-channel resolution, and prompt building."""
import json

import pytest

from channelup.config import Config, ChannelConfig, build_system_prompt, load_config
from channelup.prompts import DEFAULT_PROMPT


def _write(tmp_path, data: dict):
    p = tmp_path / "channels.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_load_minimal_channel(tmp_path, env):
    path = _write(tmp_path, {"channels": [{
        "name": "news",
        "telegram_target": "-1001",
        "rss_sources": ["https://a.com/rss", "https://b.com/rss"],
    }]})
    cfg = load_config(path, env)

    assert cfg.bot_token == "123:secret"
    assert cfg.llm_provider == "openai"
    assert cfg.llm_model == "gpt-4o-mini"
    assert cfg.admin_user_ids == frozenset({1, 2, 3})

    ch = cfg.channels[0]
    assert ch.name == "news"
    assert ch.telegram_target == "-1001"
    assert ch.rss_sources == ("https://a.com/rss", "https://b.com/rss")
    assert ch.prompt_addon == ""
    assert ch.language == "fa"          # default from file fallback
    assert ch.max_items_per_run == 5    # default
    assert ch.post_delay_seconds == 5.0


def test_load_file_defaults_and_per_channel_override(tmp_path, env):
    path = _write(tmp_path, {
        "language": "fa",
        "max_items_per_run": 4,
        "channels": [
            {"name": "a", "telegram_target": "@a", "rss_sources": ["https://a/rss"],
             "language": "en", "max_items_per_run": 9, "post_delay_seconds": 2},
            {"name": "b", "telegram_target": "@b", "rss_sources": ["https://b/rss"]},
        ],
    })
    cfg = load_config(path, env)
    a, b = cfg.channels
    assert (a.language, a.max_items_per_run, a.post_delay_seconds) == ("en", 9, 2.0)
    assert (b.language, b.max_items_per_run, b.post_delay_seconds) == ("fa", 4, 5.0)


def test_missing_required_env_raises(tmp_path):
    path = _write(tmp_path, {"channels": [{
        "name": "a", "telegram_target": "@a", "rss_sources": ["https://a/rss"]
    }]})
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        load_config(path, {})


def test_missing_channel_sources_raises(tmp_path, env):
    path = _write(tmp_path, {"channels": [{"name": "a", "telegram_target": "@a"}]})
    with pytest.raises(ValueError, match="rss_sources"):
        load_config(path, env)


def test_duplicate_channel_names_raise(tmp_path, env):
    path = _write(tmp_path, {"channels": [
        {"name": "a", "telegram_target": "@a", "rss_sources": ["https://a/rss"]},
        {"name": "a", "telegram_target": "@b", "rss_sources": ["https://b/rss"]},
    ]})
    with pytest.raises(ValueError, match="unique"):
        load_config(path, env)


def test_no_channels_raises(tmp_path, env):
    path = _write(tmp_path, {"channels": []})
    with pytest.raises(ValueError, match="at least one"):
        load_config(path, env)


def test_build_system_prompt_no_addon():
    ch = ChannelConfig(name="a", telegram_target="@a", rss_sources=("x",), language="en")
    prompt = build_system_prompt(ch)
    assert prompt == DEFAULT_PROMPT.format(language="en")
    assert "{language}" not in prompt


def test_build_system_prompt_addon_is_prepended():
    ch = ChannelConfig(name="a", telegram_target="@a", rss_sources=("x",),
                       prompt_addon="Focus on AI.", language="fa")
    prompt = build_system_prompt(ch)
    assert prompt.startswith("Focus on AI.\n\n")
    assert DEFAULT_PROMPT.format(language="fa") in prompt
    assert prompt.count("Focus on AI.") == 1