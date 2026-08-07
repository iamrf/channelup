"""Producer-Consumer pipeline built on ``asyncio.Queue`` + worker tasks.

Design
------
- **Producers**: one task per (channel, feed). Each fetches its feed on its own
  ``interval`` (seconds), dedups against the store, and routes the item:
    * ``raw``        → straight to the **publish** queue (no LLM).
    * ``custom_llm`` → to the **LLM** queue.
    * ``curate``     → accumulated in the DB; a scheduled job drains a batch.
- **LLM workers**: pull from the LLM queue, rewrite (rate-limited + concurrency
  capped), push to the publish queue.
- **Publish workers**: pull from the publish queue, acquire a per-channel token
  (strict 20 msg/min cap), then post.

``ensure_workers`` makes the pipeline usable both for the long-running bot
(``start``) and for the one-shot cron (``sweep``), without duplicating logic.
"""
from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp
from aiogram import Bot

from .config import (Config, ChannelConfig, FeedConfig, build_system_prompt, feed_prompt)
from .db import Store
from .fetcher import fetch_sources
from .llm import rewrite, select_top
from .publisher import publish
from .ratelimit import RateLimiter

log = logging.getLogger("channelup.pipeline")

MODE_RAW = "raw"
MODE_CUSTOM = "custom_llm"
MODE_CURATE = "curate"


@dataclass(frozen=True)
class LLMTask:
    channel: ChannelConfig
    feed: FeedConfig
    item: dict


@dataclass(frozen=True)
class PublishTask:
    channel: ChannelConfig
    item: dict
    text: str
    append_source: bool = True


class Pipeline:
    def __init__(self, cfg: Config, store: Store, bot: Bot,
                 http: Optional[aiohttp.ClientSession] = None) -> None:
        self.cfg = cfg
        self.store = store
        self.bot = bot
        self.http = http if http is not None else aiohttp.ClientSession()

        self.llm_queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.llm_queue_size)
        self.publish_queue: asyncio.Queue = asyncio.Queue(maxsize=cfg.publish_queue_size)

        self._publisher_rl = RateLimiter(cfg.telegram_rate_per_minute)
        self._llm_rl = RateLimiter(cfg.llm_rate_per_minute)
        self._llm_sem = asyncio.Semaphore(cfg.llm_concurrency)
        self._stop = asyncio.Event()

        self._workers: list[asyncio.Task] = []
        self._producers: list[asyncio.Task] = []
        self._workers_started = False

        self.stats: dict = {
            "started_at": None, "fetched": 0, "rewritten": 0,
            "queued_curate": 0, "curated": 0, "published": 0, "errors": 0,
        }

    # ------------------------------------------------------------- producers
    def _build_raw_text(self, feed: FeedConfig, item: dict) -> str:
        link = feed.target_link or item["link"]
        body = f"{item['title']}\n\n{item['text']}".strip()
        return f"{body}\n\n🔗 <a href=\"{html.escape(link, quote=True)}\">مشاهده</a>"

    async def fetch_feed_once(self, channel: ChannelConfig, feed: FeedConfig) -> None:
        """Fetch one feed, dedup, and route every new item to the right queue."""
        items = await asyncio.to_thread(fetch_sources, [feed.url])
        for item in items:
            if not await self.store.try_mark_seen(channel.name, item["link"], feed.mode):
                continue  # already produced before
            self.stats["fetched"] += 1
            if feed.mode == MODE_RAW:
                await self.publish_queue.put(
                    PublishTask(channel, item, self._build_raw_text(feed, item),
                                append_source=False))
            elif feed.mode == MODE_CUSTOM:
                await self.llm_queue.put(LLMTask(channel, feed, item))
            elif feed.mode == MODE_CURATE:
                await self.store.enqueue_curate(channel.name, feed.url, item)
                self.stats["queued_curate"] += 1

    async def _producer_loop(self, channel: ChannelConfig, feed: FeedConfig) -> None:
        while not self._stop.is_set():
            try:
                await self.fetch_feed_once(channel, feed)
            except Exception:
                self.stats["errors"] += 1
                log.exception("[%s/%s] fetch loop error", channel.name, feed.url)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=feed.interval)
            except asyncio.TimeoutError:
                pass

    # ----------------------------------------------------------------- curate
    async def run_curate_once(self, channel: ChannelConfig) -> int:
        """Claim a curate batch, let the LLM pick winners, rewrite, publish."""
        items = await self.store.claim_curate(channel.name, channel.curate_batch_size)
        if not items:
            return 0

        candidates = [it.to_item() for it in items]
        async with self._llm_sem:
            await self._llm_rl.acquire("llm")
            chosen = await select_top(self.http, candidates, self.cfg,
                                      channel.language, channel.curate_top_n)
        if not chosen:
            await self.store.mark_curate_processed([it.id for it in items])
            return 0

        system = build_system_prompt(channel)
        published = 0
        for it in chosen:
            async with self._llm_sem:
                await self._llm_rl.acquire("llm")
                try:
                    text = await rewrite(self.http, it, self.cfg, system)
                except Exception:
                    self.stats["errors"] += 1
                    log.exception("[%s] curate rewrite failed: %s", channel.name, it["link"])
                    continue
            await self.publish_queue.put(PublishTask(channel, it, text))
            published += 1

        self.stats["curated"] += published
        await self.store.mark_curate_processed([it.id for it in items])
        return published

    async def _curate_loop(self, channel: ChannelConfig) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=channel.curate_interval_seconds)
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self.run_curate_once(channel)
            except Exception:
                self.stats["errors"] += 1
                log.exception("[%s] curate loop error", channel.name)

    # ----------------------------------------------------------------- workers
    async def _llm_worker(self) -> None:
        while True:
            task: LLMTask = await self.llm_queue.get()
            try:
                system = feed_prompt(task.channel, task.feed)
                async with self._llm_sem:
                    await self._llm_rl.acquire("llm")
                    text = await rewrite(self.http, task.item, self.cfg, system)
                await self.publish_queue.put(PublishTask(task.channel, task.item, text))
                self.stats["rewritten"] += 1
            except Exception:
                self.stats["errors"] += 1
                log.exception("[%s] rewrite failed: %s", task.channel.name, task.item["link"])
            finally:
                self.llm_queue.task_done()

    async def _publish_worker(self) -> None:
        while True:
            task: PublishTask = await self.publish_queue.get()
            try:
                await self._publisher_rl.acquire(task.channel.telegram_target,
                                                 task.channel.rate_per_minute)
                await publish(self.bot, self.http, task.item, task.text,
                              task.channel.telegram_target, append_source=task.append_source)
                self.stats["published"] += 1
            except Exception:
                self.stats["errors"] += 1
                log.exception("[%s] publish failed: %s", task.channel.name, task.item["link"])
            finally:
                self.publish_queue.task_done()

    # -------------------------------------------------------------- lifecycle
    def ensure_workers(self) -> None:
        if self._workers_started:
            return
        self._workers_started = True
        for _ in range(self.cfg.llm_concurrency):
            self._workers.append(asyncio.create_task(self._llm_worker()))
        for _ in range(2):  # a couple of publisher workers
            self._workers.append(asyncio.create_task(self._publish_worker()))

    def start_producers(self) -> None:
        for channel in self.cfg.channels:
            for feed in channel.feeds:
                self._producers.append(asyncio.create_task(self._producer_loop(channel, feed)))
            if channel.has_curate:
                self._producers.append(asyncio.create_task(self._curate_loop(channel)))

    def start(self) -> None:
        """Long-running mode: workers + per-feed producers + curate loops."""
        self.ensure_workers()
        self.start_producers()
        self.stats["started_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    async def stop(self) -> None:
        self._stop.set()
        for t in self._producers + self._workers:
            t.cancel()
        await asyncio.gather(*(self._producers + self._workers), return_exceptions=True)
        self._producers.clear()
        self._workers.clear()
        await self.http.close()

    async def sweep(self, run_curate: bool = True) -> int:
        """One-shot: produce every feed once, run curate, drain queues. Returns published count."""
        self.ensure_workers()

        async def produce(channel: ChannelConfig, feed: FeedConfig):
            try:
                await self.fetch_feed_once(channel, feed)
            except Exception:
                self.stats["errors"] += 1
                log.exception("[%s/%s] sweep fetch error", channel.name, feed.url)

        for channel in self.cfg.channels:
            await asyncio.gather(*(produce(channel, f) for f in channel.feeds))
            if run_curate and channel.has_curate:
                await self.run_curate_once(channel)

        await self.llm_queue.join()
        await self.publish_queue.join()
        return self.stats["published"]