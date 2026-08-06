"""Orchestration: run every channel — fetch, dedup, rewrite, publish, record."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .config import Config, ChannelConfig, build_system_prompt
from .db import DedupStore
from .fetcher import fetch_sources
from .llm import rewrite
from .publisher import publish

log = logging.getLogger("channelup.runner")


@dataclass
class ChannelRunReport:
    channel: str
    fetched: int = 0
    published: int = 0
    errors: int = 0
    skipped: int = 0

    @property
    def new(self) -> int:
        return self.fetched - self.skipped


def select_new(store: DedupStore, channel: str, items: list[dict], max_items: int) -> list[dict]:
    """Return unseen items for ``channel``, capped, collapsing duplicate links."""
    fresh: list[dict] = []
    batch: set[str] = set()
    for i in items:
        if i["link"] in batch or store.is_seen(channel, i["link"]):
            continue
        batch.add(i["link"])
        fresh.append(i)
        if len(fresh) >= max_items:
            break
    return fresh


async def run_channel(cfg: Config, channel: ChannelConfig, store: DedupStore, bot: Bot) -> ChannelRunReport:
    """Process one channel end to end."""
    report = ChannelRunReport(channel=channel.name)

    items = await asyncio.to_thread(fetch_sources, list(channel.rss_sources))
    report.fetched = len(items)

    fresh = await asyncio.to_thread(select_new, store, channel.name, items,
                                    channel.max_items_per_run)
    report.skipped = len(items) - len(fresh)
    if not fresh:
        log.info("[%s] no new items (%d fetched)", channel.name, len(items))
        return report

    system_prompt = build_system_prompt(channel)
    async with aiohttp.ClientSession() as session:
        for item in fresh:
            try:
                rewritten = await rewrite(session, item, cfg, system_prompt)
                await publish(bot, session, item, rewritten, channel.telegram_target)
                await asyncio.to_thread(store.mark, channel.name, item["link"])
                report.published += 1
                await asyncio.sleep(channel.post_delay_seconds)
            except TelegramAPIError as e:
                report.errors += 1
                log.error("[%s] Telegram API error for %s: %s", channel.name, item["link"], e.message)
            except Exception:
                report.errors += 1
                log.exception("[%s] item failed: %s", channel.name, item["link"])

    log.info("[%s] done: %d/%d published (%d errors)",
             channel.name, report.published, len(fresh), report.errors)
    return report


# Cumulative counters surfaced by the /status command.
stats: dict = {"last_run": None, "published": 0, "errors": 0}


async def run_channels(cfg: Config, store: DedupStore, bot: Bot) -> list[ChannelRunReport]:
    """Run every channel; a single channel failure never aborts the others."""
    reports: list[ChannelRunReport] = []
    for channel in cfg.channels:
        try:
            reports.append(await run_channel(cfg, channel, store, bot))
        except Exception:
            reports.append(ChannelRunReport(channel=channel.name, errors=1))
            log.exception("[%s] channel run crashed", channel.name)

    stats["published"] += sum(r.published for r in reports)
    stats["errors"] += sum(r.errors for r in reports)
    stats["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return reports