"""Polling bot: admin commands (/start, /publish_now, /status) + background loop.

Entry point for the always-on mode: ``python -m channelup``. The one-shot cron mode
is ``run_cron.py`` (see DEPLOY.md).
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
from .db import DedupStore, PostgresDedupStore
from .runner import stats, run_channels

log = logging.getLogger("channelup.bot")


def loop(bot: Bot, cfg: Config, store: DedupStore) -> asyncio.Task:
    async def _run():
        while True:
            try:
                await run_channels(cfg, store, bot)
            except Exception:
                log.exception("Loop iteration crashed")
            await asyncio.sleep(cfg.publish_interval_minutes * 60)

    return asyncio.create_task(_run())


def build_dispatcher(cfg: Config, store: DedupStore) -> Dispatcher:
    dp = Dispatcher()
    dp["cfg"] = cfg
    dp["store"] = store
    admin = F.from_user.id.in_(cfg.admin_user_ids)

    @dp.message(Command("start"), admin)
    async def cmd_start(m: Message, cfg: Config):
        await m.answer(
            f"<b>ChannelUp Active</b>\n\n"
            f"<b>Channels:</b> {len(cfg.channels)}\n"
            f"<b>Interval:</b> {cfg.publish_interval_minutes}m\n"
            f"<b>LLM:</b> {cfg.llm_provider}/{cfg.llm_model}\n"
            f"<b>Sources:</b> {sum(len(c.rss_sources) for c in cfg.channels)}",
            parse_mode="HTML",
        )

    @dp.message(Command("publish_now"), admin)
    async def cmd_now(m: Message, cfg: Config, store: DedupStore, bot: Bot):
        await m.answer("Fetching & processing feeds…")
        reports = await run_channels(cfg, store, bot)
        total = sum(r.published for r in reports)
        await m.answer(f"Successfully published <b>{total}</b> item(s).", parse_mode="HTML")

    @dp.message(Command("status"), admin)
    async def cmd_status(m: Message):
        await m.answer(
            f"<b>ChannelUp Status</b>\n\n"
            f"<b>Last Run:</b> {stats['last_run'] or 'Never'}\n"
            f"<b>Total Published:</b> {stats['published']}\n"
            f"<b>Errors:</b> {stats['errors']}",
            parse_mode="HTML",
        )

    return dp


async def _async_main():
    cfg = get_config()
    store = PostgresDedupStore(cfg.database_url)
    store.init()
    bot = Bot(cfg.bot_token)
    try:
        # verify channels reachable before starting
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
        loop(bot, cfg, store)
        await build_dispatcher(cfg, store).start_polling(bot)
    finally:
        await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()