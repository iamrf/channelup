import asyncio
import logging
from aiogram import Bot
from channelup import BOT_TOKEN, run_once, init_db

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    bot = Bot(BOT_TOKEN)
    await run_once(bot)
    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())