# ChannelUp Deployment Guide (GitHub Actions + Neon DB)

This guide explains how to deploy ChannelUp as a serverless cron job using GitHub Actions and Neon PostgreSQL.

## 1. Prepare the Cron Script
The main `channelup.py` file uses `start_polling` which runs forever. For GitHub Actions, we need a script that runs exactly once and exits. 

Create a new file named `run_cron.py` in the root of your repository:

```python
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