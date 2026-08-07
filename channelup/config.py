"""Configuration: secrets from env (`.env`), feed + channel definitions from
`channels.json`.

Each channel owns one or more *feeds*. Every feed declares an RSS ``url``, a
``mode`` and a fetch ``interval`` (seconds):

- ``raw``        — bypass the LLM; post the feed's own title/text verbatim,
                   appending a configured ``target_link``.
- ``custom_llm`` — rewrite each item with the feed's ``custom_prompt``.
- ``curate``     — accumulate items in the DB; on a schedule a batch is sent to
                   the LLM which picks the most engaging and rewrites them.

Rate limits (Telegram 20 msg/min/channel cap and Gemini) are enforced per channel
and globally via token buckets (see ``ratelimit.py``).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from .prompts import DEFAULT_PROMPT

CONFIG_FILE_ENV = "CONFIG_FILE"
DEFAULT_CONFIG_FILE = "channels.json"

FEED_MODES = ("raw", "custom_llm", "curate")


@dataclass(frozen=True)
class FeedConfig:
    """One RSS source with its own cadence, mode, and optional prompt/link."""

    url: str
    interval: int                      # fetch period, seconds
    mode: str                          # raw | custom_llm | curate
    custom_prompt: str = ""            # used by custom_llm (and curate rewrite base)
    target_link: str = ""              # appended in raw mode

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> "FeedConfig":
        url = str(raw["url"])
        if not url:
            raise ValueError("feed: 'url' is required")
        mode = str(raw.get("mode", "custom_llm"))
        if mode not in FEED_MODES:
            raise ValueError(f"feed {url!r}: mode must be one of {FEED_MODES} (got {mode!r})")
        if raw.get("interval") is None:
            interval = defaults["interval"]
        else:
            interval = int(raw["interval"])
        if interval <= 0:
            raise ValueError(f"feed {url!r}: interval must be > 0")
        return cls(
            url=url,
            interval=interval,
            mode=mode,
            custom_prompt=str(raw.get("custom_prompt") or ""),
            target_link=str(raw.get("target_link") or ""),
        )


@dataclass(frozen=True)
class ChannelConfig:
    """One Telegram channel: its target, language, rate limit, and feeds."""

    name: str
    telegram_target: str
    feeds: tuple[FeedConfig, ...]
    language: str = "fa"
    prompt_addon: str = ""
    rate_per_minute: int = 19              # hard cap < Telegram's 20 msg/min
    curate_interval_seconds: int = 1800    # how often a curate batch runs
    curate_batch_size: int = 10            # items given to the LLM per batch
    curate_top_n: int = 5                  # how many of the batch get published

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> "ChannelConfig":
        feeds_raw = raw.get("feeds")
        if not isinstance(feeds_raw, (list, tuple)) or not feeds_raw:
            raise ValueError(f"channel {raw.get('name')!r}: 'feeds' must be a non-empty list")
        feeds = tuple(FeedConfig.from_dict(f, defaults) for f in feeds_raw)
        return cls(
            name=str(raw["name"]),
            telegram_target=str(raw["telegram_target"]),
            feeds=feeds,
            language=str(raw.get("language") or defaults["language"]),
            prompt_addon=str(raw.get("prompt_addon") or ""),
            rate_per_minute=int(raw.get("rate_per_minute") or defaults["rate_per_minute"]),
            curate_interval_seconds=int(raw.get("curate_interval_seconds")
                                        or defaults["curate_interval_seconds"]),
            curate_batch_size=int(raw.get("curate_batch_size")
                                  or defaults["curate_batch_size"]),
            curate_top_n=int(raw.get("curate_top_n") or defaults["curate_top_n"]),
        )

    @property
    def has_curate(self) -> bool:
        return any(f.mode == "curate" for f in self.feeds)


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_user_ids: frozenset[int]
    database_url: str
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    llm_concurrency: int
    llm_rate_per_minute: int
    telegram_rate_per_minute: int
    queue_sizes: tuple[int, int]                       # (llm_queue, publish_queue)
    channels: tuple[ChannelConfig, ...] = field(default_factory=tuple)

    @property
    def llm_queue_size(self) -> int:
        return self.queue_sizes[0]

    @property
    def publish_queue_size(self) -> int:
        return self.queue_sizes[1]


def _env_list(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def get_config(path: str | None = None) -> Config:
    """Load config from ``CONFIG_FILE`` (or ``channels.json``) plus the process env."""
    return load_config(path or os.environ.get(CONFIG_FILE_ENV) or DEFAULT_CONFIG_FILE)


def load_config(path: str, env: Mapping[str, str] = None) -> Config:
    """Parse a channels JSON file and pull global secrets from ``env`` (default os.environ)."""
    if env is None:
        env = os.environ

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "DATABASE_URL", "LLM_API_KEY") if not env.get(k)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    defaults = {
        "language": str(data.get("language") or "fa"),
        "interval": int(data.get("interval") or 300),
        "rate_per_minute": int(data.get("telegram_rate_per_minute") or 19),
        "curate_interval_seconds": int(data.get("curate_interval_seconds") or 1800),
        "curate_batch_size": int(data.get("curate_batch_size") or 10),
        "curate_top_n": int(data.get("curate_top_n") or 5),
    }

    raw_channels = data.get("channels") or []
    if not raw_channels:
        raise ValueError("channels.json: at least one 'channels' entry is required")

    channels = tuple(ChannelConfig.from_dict(c, defaults) for c in raw_channels)
    names = [c.name for c in channels]
    if len(set(names)) != len(names):
        raise ValueError(f"channels.json: channel names must be unique (got {names})")

    return Config(
        bot_token=env["TELEGRAM_BOT_TOKEN"],
        admin_user_ids=frozenset(int(x) for x in _env_list(env.get("ADMIN_USER_IDS", ""))),
        database_url=env["DATABASE_URL"],
        llm_provider=env.get("LLM_PROVIDER", "gemini").lower(),
        llm_api_key=env["LLM_API_KEY"],
        llm_model=env.get("LLM_MODEL", "gemini-2.5-flash-lite"),
        llm_base_url=env.get("LLM_API_BASE_URL", "").rstrip("/"),
        llm_concurrency=int(env.get("LLM_CONCURRENCY", "4")),
        llm_rate_per_minute=int(env.get("LLM_RATE_PER_MINUTE", "60")),
        telegram_rate_per_minute=int(data.get("telegram_rate_per_minute")
                                     or env.get("TELEGRAM_RATE_PER_MINUTE", "19")),
        queue_sizes=(
            int(data.get("llm_queue_size") or env.get("LLM_QUEUE_SIZE", "256")),
            int(data.get("publish_queue_size") or env.get("PUBLISH_QUEUE_SIZE", "256")),
        ),
        channels=channels,
    )


def build_system_prompt(channel: ChannelConfig, default: str = DEFAULT_PROMPT) -> str:
    """Channel prompt = ``prompt_addon`` prepended to the default, ``{language}`` resolved."""
    base = default.format(language=channel.language)
    return f"{channel.prompt_addon}\n\n{base}" if channel.prompt_addon else base


def feed_prompt(channel: ChannelConfig, feed: FeedConfig, default: str = DEFAULT_PROMPT) -> str:
    """Resolve the prompt an item should be rewritten with.

    A feed's ``custom_prompt`` wins (``{language}`` substituted); otherwise fall
    back to the channel's layered default prompt.
    """
    if getattr(feed, "custom_prompt", ""):
        return feed.custom_prompt.format(language=channel.language)
    return build_system_prompt(channel, default)