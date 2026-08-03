"""ChannelUp - RSS -> LLM rewrite -> Telegram channel autoposter."""

import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
import sys
import time

import feedparser
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

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
LANGUAGE = os.getenv("LANGUAGE", "en")
DB_PATH = os.getenv("DB_PATH", "channelup.db")

DEFAULT_PROMPT = """You are a professional news editor for a Telegram channel.
Rewrite the article below into an engaging original Telegram post.
- Write in {language}
- 2-3 concise paragraphs, journalistic tone
- Add relevant emojis and 3-5 hashtags at the end
- Plain text only: never use *, _, `, [, ] or any Markdown
- Under 3000 characters"""
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

def image_of(entry) -> str | None:
    for m in entry.get("media_content", []) + entry.get("media_thumbnail", []):
        if m.get("url"):
            return m["url"]
    for e in entry.get("enclosures", []):
        if e.get("type", "").startswith("image/"):
            return e.get("href")
    if m := re.search(r'<img[^>]+src="([^"]+)"', entry.get("summary", "")):
        return m.group(1)
    return None

def fetch_new() -> list[dict]:
    """Blocking; run via asyncio.to_thread. Returns unseen items, oldest first."""
    items = []
    for url in RSS_SOURCES:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries:
                link = e.get("link") or e.get("id")
                if not link or seen(link):
                    continue
                body = clean(e.get("content", [{}])[0].get("value") or e.get("summary", ""))
                items.append({
                    "title": clean(e.get("title", "")),
                    "link": link,
                    "text": body[:4000],
                    "image": image_of(e),
                })
        except Exception:
            log.exception("fetch failed: %s", url)
    return items[:MAX_PER_RUN]

# --- LLM --------------------------------------------------------------------
# ponytail: single retry-free-ish loop; if you need per-provider tuning, split later.
async def rewrite(session, item: dict) -> str:
    user = f"Title: {item['title']}\n\n{item['text']}\n\nSource: {item['link']}"
    system = SYSTEM_PROMPT.format(language=LANGUAGE)
    if PROVIDER == "gemini":
        base = LLM_BASE or "https://generativelanguage.googleapis.com/v1beta"
        url = f"{base}/models/{LLM_MODEL}:generateContent"
        headers = {"x-goog-api-key": LLM_KEY}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
        }
        pick = lambda d: d["candidates"][0]["content"]["parts"][0]["text"]
    else:  # openai / deepseek / openrouter — all OpenAI-compatible
        base = LLM_BASE or {
            "openai": "https://api.openai.com/v1",
            "deepseek": "https://api.deepseek.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
        }[PROVIDER]
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {LLM_KEY}"}
        payload = {"model": LLM_MODEL, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]}
        pick = lambda d: d["choices"][0]["message"]["content"]

    status = None
    for attempt in range(3):
        async with session.post(url, json=payload, headers=headers) as r:
            status = r.status
            if status in (429, 500, 502, 503):
                await asyncio.sleep(2 ** attempt * 5)
                continue
            r.raise_for_status()
            return pick(await r.json()).strip()
    raise RuntimeError(f"LLM unavailable after retries (HTTP {status})")

# --- publish ----------------------------------------------------------------
async def publish(bot: Bot, item: dict, text: str) -> None:
    body = f"{text}\n\n🔗 {item['link']}"
    for chat in CHANNELS:
        if item["image"]:
            try:
                await bot.send_photo(chat, item["image"], caption=body[:1024])
                continue
            except Exception:
                log.warning("photo failed, falling back to text: %s", item["image"])
        await bot.send_message(chat, body[:4096], disable_web_page_preview=False)

stats = {"last_run": None, "published": 0, "errors": 0}

async def run_once(bot: Bot) -> int:
    items = await asyncio.to_thread(fetch_new)
    session = await bot.session.create_session()
    n = 0
    for item in items:
        try:
            await publish(bot, item, await rewrite(session, item))
            mark(item["link"])  # only after a successful post, so failures retry next run
            n += 1
            await asyncio.sleep(POST_DELAY)
        except Exception:
            stats["errors"] += 1
            log.exception("item failed: %s", item["link"])
    stats["last_run"], stats["published"] = time.strftime("%Y-%m-%d %H:%M:%S"), stats["published"] + n
    log.info("run done: %d/%d published", n, len(items))
    return n

# --- bot --------------------------------------------------------------------
dp = Dispatcher()
admin = F.from_user.id.in_(ADMINS)

@dp.message(Command("start"), admin)
async def cmd_start(m: Message):
    await m.answer(f"ChannelUp running.\nSources: {len(RSS_SOURCES)}\nChannels: {len(CHANNELS)}\n"
                   f"Interval: {INTERVAL // 60}m\nProvider: {PROVIDER}/{LLM_MODEL}")

@dp.message(Command("publish_now"), admin)
async def cmd_now(m: Message, bot: Bot):
    await m.answer("Fetching…")
    await m.answer(f"Published {await run_once(bot)} item(s).")

@dp.message(Command("status"), admin)
async def cmd_status(m: Message):
    await m.answer(f"Last run: {stats['last_run']}\nPublished: {stats['published']}\nErrors: {stats['errors']}")

async def loop(bot: Bot):
    while True:
        try:
            await run_once(bot)
        except Exception:
            log.exception("run_once crashed")
        await asyncio.sleep(INTERVAL)

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())