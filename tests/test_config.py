"""Config parsing: multi-mode fe ed schema, defaults, validation, prompt resolution."""
import json

import pytest

from channelup.config import (Config, ChannelConfig, FeedConfig, build_system_prompt,
                              channel_prompt_text, feed_prompt, load_config)
from channelup.prompts import DEFAULT_PROMPT


def _write(tmp_path, data: dict):
    p = tmp_path / "channels.json"
    p.write_text(json.dumps(data))
    return str(p)


def test_load_global_defaults_and_feed_modes(tmp_path, env):
    path = _write(tmp_path, {"interval": 600, "channels": [{
        "name": "tech", "telegram_target": "-1001",
        "feeds": [
            {"url": "https://raw/rss", "mode": "raw", "target_link": "https://t.me/x"},
            {"url": "https://llm/rss", "mode": "custom_llm",
             "custom_prompt": "Focus on X."},
            {"url": "https://cur/rss", "mode": "curate"},
        ],
    }]})
    cfg = load_config(path, env)
    assert cfg.bot_token == "123:secret"
    assert len(cfg.channels) == 1
    feeds = cfg.channels[0].feeds
    assert [f.mode for f in feeds] == ["raw", "custom_llm", "curate"]
    assert feeds[0].target_link == "https://t.me/x"
    # feed without explicit interval inherits the global default (600)
    assert feeds[0].interval == 600


def test_raw_feed_empty_target_link_allowed(tmp_path, env):
    path = _write(tmp_path, {"channels": [{
        "name": "a", "telegram_target": "@a",
        "feeds": [{"url": "https://raw/rss", "mode": "raw"}],
    }]})
    ch = load_config(path, env).channels[0]
    assert ch.feeds[0].mode == "raw"
    assert ch.feeds[0].target_link == ""


def test_invalid_mode_rejected(tmp_path, env):
    path = _write(tmp_path, {"channels": [{
        "name": "a", "telegram_target": "@a",
        "feeds": [{"url": "https://x/rss", "mode": "bogus"}],
    }]})
    with pytest.raises(ValueError, match="mode"):
        load_config(path, env)


def test_zero_interval_rejected(tmp_path, env):
    path = _write(tmp_path, {"channels": [{
        "name": "a", "telegram_target": "@a",
        "feeds": [{"url": "https://x/rss", "mode": "raw", "interval": 0}],
    }]})
    with pytest.raises(ValueError, match="interval"):
        load_config(path, env)


def test_channel_requires_feeds(tmp_path, env):
    path = _write(tmp_path, {"channels": [{"name": "a", "telegram_target": "@a"}]})
    with pytest.raises(ValueError, match="feeds"):
        load_config(path, env)


def test_duplicate_channel_names_rejected(tmp_path, env):
    path = _write(tmp_path, {"channels": [
        {"name": "a", "telegram_target": "@a", "feeds": [{"url": "u", "mode": "raw"}]},
        {"name": "a", "telegram_target": "@b", "feeds": [{"url": "v", "mode": "raw"}]},
    ]})
    with pytest.raises(ValueError, match="unique"):
        load_config(path, env)


def test_missing_required_env_raises(tmp_path):
    path = _write(tmp_path, {"channels": [{
        "name": "a", "telegram_target": "@a",
        "feeds": [{"url": "u", "mode": "raw"}],
    }]})
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        load_config(path, {})


def test_has_curate_flag():
    ch = make_channel_with_modes(["raw", "curate"])
    assert ch.has_curate is True
    ch2 = make_channel_with_modes(["raw", "custom_llm"])
    assert ch2.has_curate is False


def make_channel_with_modes(modes, language="en"):
    return ChannelConfig(
        name="c", telegram_target="@c", language=language,
        feeds=tuple(FeedConfig(url=f"https://{m}/rss", interval=300, mode=m) for m in modes),
    )


def test_feed_prompt_layers_custom_over_channel_over_default():
    ch = ChannelConfig(name="c", telegram_target="@c",
                       feeds=(FeedConfig("u", 300, "custom_llm"),), language="en")
    feed = FeedConfig(url="u", interval=300, mode="custom_llm",
                      custom_prompt="Be brief in {language}.")
    p = feed_prompt(ch, feed)
    chan_prompt = channel_prompt_text(ch)
    assert p.startswith("Be brief in en.\n\n")
    assert chan_prompt in p
    assert "{language}" not in p


def test_feed_prompt_falls_back_to_channel_prompt():
    ch = make_channel_with_modes(["raw"])
    feed = FeedConfig(url="u", interval=300, mode="custom_llm")  # no custom_prompt
    p = feed_prompt(ch, feed)
    assert DEFAULT_PROMPT.format(language="en") in p


def test_feed_prompt_three_layer_chain():
    ch = ChannelConfig(name="c", telegram_target="@c",
                       feeds=(FeedConfig("u", 300, "custom_llm"),),
                       channel_prompt="Channel flavor.", language="en")
    feed = FeedConfig(url="u", interval=300, mode="custom_llm", custom_prompt="Feed angle.")
    p = feed_prompt(ch, feed)
    # most-specific layer first, channel layer, then default
    assert p.index("Feed angle.") < p.index("Channel flavor.") < p.index(DEFAULT_PROMPT[:20])


def test_load_jsonc_with_comments_and_trailing_commas(tmp_path, env):
    """load_config tolerates // comments (incl. inline after values) + trailing commas."""
    path = tmp_path / "c.json"
    path.write_text(
        """
        // channelup config
        { "interval": 120,  // default fetch period
          "channels": [
            { "name": "a", "telegram_target": "@a",  // target
              "feeds": [
                { "url": "https://x/rss", "mode": "raw",
                  "target_link": "https://t.me/a", },   // trailing comma ok
                { "url": "https://y/rss", "mode": "curate", }
              ],
            },
          ],
        }
        """
    )
    cfg = load_config(str(path), env)
    assert cfg.channels[0].feeds[0].mode == "raw"
    assert cfg.channels[0].feeds[0].interval == 120
    assert len(cfg.channels[0].feeds) == 2


def test_build_system_prompt_addon():
    ch = ChannelConfig(name="c", telegram_target="@c",
                       feeds=(FeedConfig("u", 300, "raw"),),
                       channel_prompt="Add hashtags.", language="fa")
    p = build_system_prompt(ch)
    assert p.startswith("Add hashtags.\n\n")
    assert DEFAULT_PROMPT.format(language="fa") in p
    assert "{language}" not in p