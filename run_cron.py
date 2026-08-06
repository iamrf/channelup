"""One-shot cron runner: process every channel once and exit.

Used (exactly one) by:
- GitHub Actions scheduled workflow (serverless):  ``python run_cron.py``
- Ubuntu self-host systemd timer / cron:           ``run_cron.py``

It posts to Telegram directly, so the caller must provide the bot token, DB and
LLM keys (via .env on the server, or repo secrets as env vars in CI).
"""
import asyncio
import logging

from aiogram import Bot
from dotenv import load_dotenv

from channelup.config import get_config
from channelup.db import PostgresDedupStore
from channelup.runner import run_channels


async def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()

    cfg = get_config()
    store = PostgresDedupStore(cfg.database_url)
    store.init()

    bot = Bot(cfg.bot_token)
    try:
        reports = await run_channels(cfg, store, bot)
        for r in reports:
            logging.info("[%s] fetched=%d published=%d errors=%d",
                         r.channel, r.fetched, r.published, r.errors)
        return sum(r.published for r in reports)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())