"""Polling bot: admin commands + start the producer-consumer pipeline.

Entry point for the always-on mode: ``python -m channelup``. The one-shot cron
mode is ``run_cron.py`` (see DEPLOY.md).
"""
from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

from .config import Config, get_config
from .db import PostgresStore, Store
from .pipeline import Pipeline

log = logging.getLogger("channelup.bot")


def build_dispatcher(cfg: Config, store: Store, pipeline: Pipeline) -> Dispatcher:
    dp = Dispatcher()
    dp["cfg"] = cfg
    dp["pipeline"] = pipeline
    admin = F.from_user.id.in_(cfg.admin_user_ids)

    @dp.message(Command("start"), admin)
    async def cmd_start(m: Message, cfg: Config):
        n_feeds = sum(len(c.feeds) for c in cfg.channels)
        await m.answer(
            f"<b>ChannelUp Active</b>\n\n"
            f"<b>Channels:</b> {len(cfg.channels)}\n"
            f"<b>Feeds:</b> {n_feeds}\n"
            f"<b>LLM:</b> {cfg.llm_provider}/{cfg.llm_model}\n"
            f"<b>Telegram cap:</b> {cfg.telegram_rate_per_minute}/min",
            parse_mode="HTML",
        )

    @dp.message(Command("publish_now"), admin)
    async def cmd_now(m: Message, pipeline: Pipeline):
        await m.answer("Fetching & processing feeds…")
        before = pipeline.stats["published"]
        try:
            await pipeline.sweep()
            published = pipeline.stats["published"] - before
            await m.answer(f"Published <b>{published}</b> new item(s).", parse_mode="HTML")
        except Exception:
            log.exception("publish_now failed")
            await m.answer("Sweep failed — see logs.", parse_mode="HTML")

    @dp.message(Command("status"), admin)
    async def cmd_status(m: Message, pipeline: Pipeline):
        s = pipeline.stats
        await m.answer(
            f"<b>ChannelUp Status</b>\n\n"
            f"<b>Started:</b> {s['started_at'] or 'Never'}\n"
            f"<b>Fetched:</b> {s['fetched']}\n"
            f"<b>Rewritten:</b> {s['rewritten']}\n"
            f"<b>Curated:</b> {s['curated']}\n"
            f"<b>Published:</b> {s['published']}\n"
            f"<b>Errors:</b> {s['errors']}",
            parse_mode="HTML",
        )

    return dp


async def _async_main() -> None:
    cfg = get_config()
    store = PostgresStore(cfg.database_url)
    await store.init()
    bot = Bot(cfg.bot_token)
    pipeline: Pipeline | None = None
    try:
        me = await bot.get_me()
        for ch in cfg.channels:
            try:
                info = await bot.get_chat(ch.telegram_target)
                log.info("Channel verified: %s (%s)", info.title or ch.telegram_target, ch.name)
            except TelegramAPIError as e:
                sys.exit(
                    f"Cannot reach channel {ch.telegram_target!r}: {e.message}\n"
                    f"  - Numeric IDs must look like -1001234567890 (not 1234567890)\n"
                    f"  - Add @{me.username} to the channel as admin with 'Post Messages' rights\n"
                    f"  - Private channels have no @username; use the numeric ID"
                )

        pipeline = Pipeline(cfg, store, bot)
        pipeline.start()
        dp = build_dispatcher(cfg, store, pipeline)
        await dp.start_polling(bot)
    finally:
        if pipeline is not None:
            await pipeline.stop()
        await store.close()
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()