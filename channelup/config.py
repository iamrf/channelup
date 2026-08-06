"""Configuration: global secrets come from env (`.env`), channel definitions come
from a JSON file (`channels.json`, non-secret). Each channel carries its own RSS
sources, an optional prompt add-on, and optional per-channel tuning.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping

from .prompts import DEFAULT_PROMPT

CONFIG_FILE_ENV = "CONFIG_FILE"
DEFAULT_CONFIG_FILE = "channels.json"


@dataclass(frozen=True)
class ChannelConfig:
    """One Telegram target channel with its own sources / prompt / tuning."""

    name: str
    telegram_target: str
    rss_sources: tuple[str, ...]
    prompt_addon: str = ""            # prepended to the default prompt
    language: str = "fa"
    max_items_per_run: int = 5
    post_delay_seconds: float = 5.0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], defaults: Mapping[str, Any]) -> "ChannelConfig":
        sources = raw.get("rss_sources") or ()
        if not isinstance(sources, (list, tuple)) or not sources:
            raise ValueError(f"channel {raw.get('name')!r}: rss_sources must be a non-empty list")
        return cls(
            name=str(raw["name"]),
            telegram_target=str(raw["telegram_target"]),
            rss_sources=tuple(str(s) for s in sources),
            prompt_addon=str(raw.get("prompt_addon") or ""),
            language=str(raw.get("language") or defaults["language"]),
            max_items_per_run=int(raw.get("max_items_per_run") or defaults["max_items_per_run"]),
            post_delay_seconds=float(raw.get("post_delay_seconds") or defaults["post_delay_seconds"]),
        )


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_user_ids: frozenset[int]
    database_url: str
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    publish_interval_minutes: int
    channels: tuple[ChannelConfig, ...] = field(default_factory=tuple)


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

    # Required globals (secrets live only in env, never committed).
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "DATABASE_URL", "LLM_API_KEY") if not env.get(k)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")

    defaults = {
        "language": str(data.get("language") or "fa"),
        "max_items_per_run": int(data.get("max_items_per_run") or 5),
        "post_delay_seconds": float(data.get("post_delay_seconds") or 5.0),
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
        llm_model=env.get("LLM_MODEL", "gemini-2.0-flash"),
        llm_base_url=env.get("LLM_API_BASE_URL", "").rstrip("/"),
        publish_interval_minutes=int(env.get("PUBLISH_INTERVAL_MINUTES", "30")),
        channels=channels,
    )


def build_system_prompt(channel: ChannelConfig, default: str = DEFAULT_PROMPT) -> str:
    """Combine a channel's prompt add-on with the default prompt.

    The add-on is prepended so it reads as an instruction layered on top of the
    base editorial style. ``{language}`` is substituted per channel.
    """
    base = default.format(language=channel.language)
    return f"{channel.prompt_addon}\n\n{base}" if channel.prompt_addon else base