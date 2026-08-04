"""ChannelUp - RSS -> LLM rewrite -> Telegram channel autoposter."""

import asyncio
import hashlib
import html
import io
import logging
import os
import re
import sqlite3
import sys
import time
from typing import Optional

import aiohttp
import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from dotenv import load_dotenv

# ایمپورت کردن پرامپت از ماژول جدید
from prompts import DEFAULT_PROMPT

load_dotenv()
log = logging.getLogger("channelup")

# --- config -----------------------------------------------------------------
def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHANNELS = _list("TARGET_CHANNEL_IDS")
RSS_SOURCES = _list("RSS_SOURCES")
ADMINS = {int(x) for x in _list("ADMIN_USER_IDS")}
PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()
LLM_KEY = os.environ["LLM_API_KEY"]
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-2.0-flash")
LLM_BASE = os.getenv("LLM_API_BASE_URL", "").rstrip("/")
INTERVAL = int(os.getenv("PUBLISH_INTERVAL_MINUTES", "30")) * 60
POST_DELAY = int(os.getenv("POST_DELAY_SECONDS", "5"))
MAX_PER_RUN = int(os.getenv("MAX_ITEMS_PER_RUN", "5"))
LANGUAGE = os.getenv("LANGUAGE", "fa")
DB_PATH = os.getenv("DB_PATH", "channelup.db")

SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT") or (
    open(p, encoding="utf-8").read() if (p := os.getenv("SYSTEM_PROMPT_FILE")) else DEFAULT_PROMPT
)

if not CHANNELS or not RSS_SOURCES:
    sys.exit("TARGET_CHANNEL_IDS and RSS_SOURCES must be set")

# --- dedup store ------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA journal_mode=WAL")
db.execute("CREATE TABLE IF NOT EXISTS published (hash TEXT PRIMARY KEY, ts INTEGER)")
db.commit()

def _h(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

def seen(key: str) -> bool:
    return db.execute("SELECT 1 FROM published WHERE hash=?", (_h(key),)).fetchone() is not None

def mark(key: str) -> None:
    db.execute("INSERT OR IGNORE INTO published VALUES (?,?)", (_h(key), int(time.time())))
    db.commit()

# --- fetch ------------------------------------------------------------------
TAGS = re.compile(r"<[^>]+>")
WS = re.compile(r"[ \t]{2,}")

def clean(raw: str) -> str:
    return WS.sub(" ", html.unescape(TAGS.sub(" ", raw or ""))).strip()

def image_of(entry) -> Optional[str]:
    for m in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if m.get("url"):
            return m["url"]
    for e in entry.get("enclosures", []):
        if e.get("type", "").startswith("image/"):
            return e.get("href")
    if m := re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "")):
        return m.group(1)
    return None

def parse_feed(source: str) -> list[dict]:
    """Blocking, runs in a worker thread. Must NOT touch the sqlite connection."""
    out = []
    parsed = feedparser.parse(source)
    for e in parsed.entries:
        link = e.get("link") or e.get("id")
        if not link:
            continue
        body = clean((e.get("content") or [{}])[0].get("value") or e.get("summary", ""))
        out.append({
            "title": clean(e.get("title", "")),
            "link": link,
            "text": body[:4000],
            "image": image_of(e),
        })
    return out

def fetch_all() -> list[dict]:
    items = []
    for url in RSS_SOURCES:
        try:
            items += parse_feed(url)
        except Exception:
            log.exception("fetch failed: %s", url)
    return items

def select_new(items: list[dict]) -> list[dict]:
    """Event-loop thread only (reads DB). Also drops links duplicated across feeds."""
    fresh, batch = [], set()
    for i in items:
        if i["link"] in batch or seen(i["link"]):
            continue
        batch.add(i["link"])
        fresh.append(i)
        if len(fresh) >= MAX_PER_RUN:
            break
    return fresh

# --- LLM --------------------------------------------------------------------
async def rewrite(session: aiohttp.ClientSession, item: dict) -> str:
    user = f"Title: {item['title']}\n\n{item['text']}\n\nSource: {item['link']}"
    system = SYSTEM_PROMPT.format(language=LANGUAGE)
    
    if PROVIDER == "gemini":
        base = LLM_BASE or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{LLM_MODEL}:generateContent"
        headers = {"x-goog-api-key": LLM_KEY, "Content-Type": "application/json"}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        pick = lambda d: d["candidates"][0]["content"]["parts"][0]["text"]
    else:  # openai / deepseek / openrouter — OpenAI-compatible
        base = LLM_BASE or {
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        }.get(PROVIDER, "https://api.openai.com/v1")
        
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {LLM_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        }
        pick = lambda d: d["choices"][0]["message"]["content"]

    status = None
    for attempt in range(3):
        try:
            async with session.post(url, json=payload, headers=headers, timeout=30) as r:
                status = r.status
                if status in (429, 500, 502, 503, 504):
                    await asyncio.sleep(2 ** attempt * 5)
                    continue
                r.raise_for_status()
                data = await r.json()
                return pick(data).strip()
        except asyncio.TimeoutError:
            log.warning(f"LLM request timed out (attempt {attempt + 1})")
            await asyncio.sleep(2 ** attempt * 3)
        except Exception as e:
            if attempt == 2:
                raise e
            await asyncio.sleep(2 ** attempt * 3)
            
    raise RuntimeError(f"LLM unavailable after retries (HTTP {status})")

# --- publish helpers --------------------------------------------------------
async def fetch_image_bytes(session: aiohttp.ClientSession, url: str) -> Optional[bytes]:
    try:
        async with session.get(url, timeout=10) as resp:
            if resp.status == 200:
                content_type = resp.headers.get("Content-Type", "")
                if content_type.startswith("image/") or url.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
                    return await resp.read()
    except Exception as e:
        log.warning("Failed to download image %s: %s", url, e)
    return None

async def publish(bot: Bot, session: aiohttp.ClientSession, item: dict, text: str) -> None:
    # حذف تگ‌های غیرمجازی که ممکن است هوش مصنوعی تولید کند
    safe_text = text.replace("<p>", "").replace("</p>", "\n\n").replace("<br>", "\n").replace("<br/>", "\n")
    
    body = f"{safe_text}\n\n🔗 <a href=\"{item['link']}\">منبع خبر</a>"

    for chat in CHANNELS:
        sent_with_photo = False
        
        if item["image"]:
            img_bytes = await fetch_image_bytes(session, item["image"])
            if img_bytes:
                try:
                    if len(body) <= 1024:
                        photo_file = BufferedInputFile(img_bytes, filename="image.jpg")
                        await bot.send_photo(chat, photo_file, caption=body, parse_mode="HTML")
                        sent_with_photo = True
                    else:
                        photo_file = BufferedInputFile(img_bytes, filename="image.jpg")
                        await bot.send_photo(chat, photo_file)
                        await bot.send_message(chat, body[:4096], parse_mode="HTML", disable_web_page_preview=True)
                        sent_with_photo = True
                except TelegramAPIError as e:
                    if "chat not found" in str(e).lower():
                        raise
                    log.warning("Photo sending failed (%s), sending as text", e.message)

        if not sent_with_photo:
            await bot.send_message(chat, body[:4096], parse_mode="HTML", disable_web_page_preview=False)

stats = {"last_run": None, "published": 0, "errors": 0}

async def run_once(bot: Bot) -> int:
    items = select_new(await asyncio.to_thread(fetch_all))
    if not items:
        log.info("No new items found")
        return 0

    n = 0
    async with aiohttp.ClientSession() as session:
        for item in items:
            try:
                rewritten_text = await rewrite(session, item)
                await publish(bot, session, item, rewritten_text)
                mark(item["link"])
                n += 1
                await asyncio.sleep(POST_DELAY)
            except TelegramAPIError as e:
                stats["errors"] += 1
                log.error("Telegram API error for %s: %s", item["link"], e.message)
            except Exception:
                stats["errors"] += 1
                log.exception("Item failed: %s", item["link"])
                
    stats["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    stats["published"] += n
    log.info("Run completed: %d/%d published", n, len(items))
    return n

# --- bot handlers -----------------------------------------------------------
dp = Dispatcher()
admin = F.from_user.id.in_(ADMINS)

@dp.message(Command("start"), admin)
async def cmd_start(m: Message):
    await m.answer(
        f"<b>ChannelUp Bot Active</b>\n\n"
        f"<b>Sources:</b> {len(RSS_SOURCES)}\n"
        f"<b>Channels:</b> {len(CHANNELS)}\n"
        f"<b>Interval:</b> {INTERVAL // 60}m\n"
        f"<b>LLM:</b> {PROVIDER}/{LLM_MODEL}",
        parse_mode="HTML"
    )

@dp.message(Command("publish_now"), admin)
async def cmd_now(m: Message, bot: Bot):
    await m.answer("Fetching & processing feeds…")
    count = await run_once(bot)
    await m.answer(f"Successfully published <b>{count}</b> item(s).", parse_mode="HTML")

@dp.message(Command("status"), admin)
async def cmd_status(m: Message):
    await m.answer(
        f"<b>ChannelUp Status</b>\n\n"
        f"<b>Last Run:</b> {stats['last_run'] or 'Never'}\n"
        f"<b>Total Published:</b> {stats['published']}\n"
        f"<b>Errors Encountered:</b> {stats['errors']}",
        parse_mode="HTML"
    )

async def check_channels(bot: Bot) -> None:
    me = await bot.get_me()
    for chat in CHANNELS:
        try:
            info = await bot.get_chat(chat)
            log.info("Channel verified: %s (%s)", info.title or chat, chat)
        except TelegramAPIError as e:
            sys.exit(
                f"Cannot reach channel {chat!r}: {e.message}\n"
                f"  - Numeric IDs must look like -1001234567890 (not 1234567890)\n"
                f"  - Add @{me.username} to the channel as admin with 'Post Messages' rights\n"
                f"  - Private channels have no @username; use the numeric ID"
            )

async def loop(bot: Bot):
    while True:
        try:
            await run_once(bot)
        except Exception:
            log.exception("Loop iteration crashed")
        await asyncio.sleep(INTERVAL)

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(BOT_TOKEN)
    await check_channels(bot)
    asyncio.create_task(loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())