"""Render Web Service entrypoint.

Render deploys ChannelUp as a long-running **web** service. This module:

  1. starts a tiny aiohttp health server on ``$PORT`` so Render sees a healthy
     listener (an HTTP service that serves nothing is marked unhealthy),
  2. boots the producer-consumer pipeline (per-feed producers, LLM/publish
     workers, curate), and
  3. polls the bot, so admin commands (``/start``, ``/publish_now``, ``/status``)
     still work.

Render's filesystem is ephemeral — every deploy starts from the repo — so
``DATABASE_URL`` must point at a real Neon database. See DEPLOY.md → "Option C".
"""
from __future__ import annotations

import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot
from dotenv import load_dotenv

from channelup.bot import build_dispatcher
from channelup.config import get_config
from channelup.db import PostgresStore
from channelup.pipeline import Pipeline

log = logging.getLogger("channelup.render")


async def _health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()

    # 1. health server (Render expects a listening port)
    port = int(os.environ.get("PORT", "10000"))
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("health server listening on :%d", port)

    # 2+3. pipeline + bot polling
    cfg = get_config()
    store = PostgresStore(cfg.database_url)
    await store.init()
    bot = Bot(cfg.bot_token)
    pipeline = Pipeline(cfg, store, bot)
    pipeline.start()
    try:
        dp = build_dispatcher(cfg, store, pipeline)
        await dp.start_polling(bot)
    finally:
        await pipeline.stop()
        await store.close()
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())