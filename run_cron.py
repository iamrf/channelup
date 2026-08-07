"""One-shot cron runner: sweep every feed once, drain the queues, exit.

Used (exactly one) by the GitHub Actions scheduled workflow and the Ubuntu
systemd timer. Runs the producer-consumer ``Pipeline.sweep``: fetch each feed
once (+ run curate once), then wait for the LLM and publish queues to drain.
"""
import asyncio
import logging

from aiogram import Bot
from dotenv import load_dotenv

from channelup.config import get_config
from channelup.db import PostgresStore
from channelup.pipeline import Pipeline


async def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()

    cfg = get_config()
    store = PostgresStore(cfg.database_url)
    await store.init()

    bot = Bot(cfg.bot_token)
    pipeline: Pipeline | None = None
    try:
        pipeline = Pipeline(cfg, store, bot)
        published = await pipeline.sweep()
        log = logging.getLogger("channelup.cron")
        log.info("sweep done: fetched=%d rewritten=%d curated=%d published=%d errors=%d",
                 pipeline.stats["fetched"], pipeline.stats["rewritten"],
                 pipeline.stats["curated"], pipeline.stats["published"],
                 pipeline.stats["errors"])
        return published
    finally:
        if pipeline is not None:
            await pipeline.stop()
        await store.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())