# ChannelUp — Automated Telegram Channel Content Curator

## Architecture

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  RSS/API    │───▶│  Fetcher     │───▶│  AI Processor │───▶│  Publisher   │
│  Sources    │    │  (feedparser │    │  (LLM API)   │    │  (aiogram)   │
│             │    │   + aiohttp) │    │  summarize   │    │  send to     │
│             │    │              │    │  translate   │    │  channel     │
│             │    │              │    │  format      │    │              │
└─────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
                          │                    │                   │
                          ▼                    ▼                   ▼
                   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
                   │  SQLite DB   │    │  .env config │    │  Telegram    │
                   │  dedup cache │    │  system      │    │  Channel(s)  │
                   │  published   │    │  prompt      │    │              │
                   └──────────────┘    └──────────────┘    └──────────────┘
                          ▲
                          │
                   ┌──────────────┐
                   │  APScheduler │
                   │  every 30min │
                   │  or manual   │
                   └──────────────┘
```

## Project Structure

```
channelup/
├── .env.example
├── AGENTS.md
├── DEPLOY.md
├── README.md
├── requirements.txt
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point, scheduler setup
│   ├── config.py            # Env parsing, pydantic-settings
│   ├── fetcher.py           # RSS/API fetch + parse
│   ├── ai_processor.py      # LLM call + system prompt
│   ├── publisher.py         # Telegram formatting + send
│   ├── db.py                # SQLite dedup via aiosqlite
│   └── models.py            # Typed dataclasses
└── tests/
    └── test_fetcher.py
```

## Key Design Decisions

1. **aiogram v3** — async-first, native Telegram API, Polling/Webhook ready
2. **APScheduler AsyncIOScheduler** — cron-like intervals, no external dependency
3. **aiosqlite** — lightweight, no DB server, portable
4. **httpx** — async HTTP for LLM API calls
5. **pydantic-settings** — typed `.env` loading, validation
6. **MarkdownV2** — Telegram-compatible formatting, escape helpers

## Data Flow

1. **Scheduler** fires every N minutes (configurable via env)
2. **Fetcher** reads each RSS source, returns list of items (title, link, summary, pub_date)
3. **DB** checks if item link hash already exists; skip if yes
4. **AI Processor** sends item text + system prompt to LLM API, gets back formatted post
5. **Publisher** escapes MarkdownV2 special chars, sends to each target channel
6. **DB** records published item hash, prevents re-publish

## Components Detail

### config.py
`pydantic.BaseSettings`:
- `TELEGRAM_BOT_TOKEN`
- `TARGET_CHANNEL_IDS` (comma-separated)
- `LLM_PROVIDER` (gemini | deepseek | openai | custom)
- `LLM_API_KEY`
- `LLM_API_BASE_URL` (for custom endpoint)
- `LLM_MODEL`
- `RSS_SOURCES` (comma-separated URLs)
- `PUBLISH_INTERVAL_MINUTES`
- `SYSTEM_PROMPT_FILE` (optional path to custom prompt)
- `LANGUAGE` (default: `fa` for Persian)

### fetcher.py
`async def fetch_all_sources() -> list[Article]`:
- `feedparser` for RSS
- `httpx.AsyncClient` for API-based sources
- Parse into `Article` dataclass

### ai_processor.py
`async def process_article(article: Article) -> str`:
- Build prompt: system + article text
- Call LLM API based on provider
- Return formatted MarkdownV2 string
- Customizable system prompt template

### publisher.py
`async def publish_to_channels(bot, text: str)`:
- Escape MarkdownV2 special chars
- `bot.send_message(chat_id=channel_id, text=..., parse_mode="MarkdownV2")`
- Error handling for parse failures → fallback to plain text

### db.py
`async def is_published(item_hash: str) -> bool`, `async def mark_published(item_hash: str)`:
- SQLite table `published_items` with `hash TEXT PRIMARY KEY`
- `aiosqlite` connection pool

### main.py
`async def main()`:
- Initialize bot, dp, db
- APScheduler adds job `fetch_and_publish`
- `dp.run_polling()`
- `/start` — welcome message
- `/publish_now` — manual trigger (admin only, via user ID whitelist)
- `/status` — last run stats

## Environment Variables

```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TARGET_CHANNEL_IDS=-1001234567890
LLM_PROVIDER=gemini
LLM_API_KEY=AIzaSy...
LLM_MODEL=gemini-2.0-flash
LLM_API_BASE_URL=
RSS_SOURCES=https://www.varzesh3.com/rss/all,https://www.bbc.com/persian
PUBLISH_INTERVAL_MINUTES=30
LANGUAGE=fa
ADMIN_USER_IDS=123456789
SYSTEM_PROMPT=
```

## System Prompt Template (default)

```
You are a professional news editor for a Telegram channel. 
Your task: rewrite the following news article into an engaging Telegram post.
Requirements:
- Write in {language}
- Summarize key points concisely (2-3 paragraphs)
- Add relevant emojis
- Add 3-5 relevant hashtags at the end
- Use clear, journalistic tone
- IMPORTANT: Do NOT use Markdown formatting characters like *, _, `, [, ], etc.
- Use plain text only
- Keep under 4000 characters
```

## Rate Limiting & Error Handling

- LLM API: exponential backoff on 429/503, max 3 retries
- Telegram: aiogram handles 429 (FloodWait) automatically
- RSS: skip source on failure, log error, continue others
- DB: WAL mode for concurrent safety

## Files to Write

| File | Purpose |
|------|---------|
| `.env.example` | Template for environment variables |
| `requirements.txt` | Python dependencies |
| `src/__init__.py` | Package init |
| `src/config.py` | Typed env config via pydantic-settings |
| `src/models.py` | Article dataclass |
| `src/db.py` | SQLite dedup cache |
| `src/fetcher.py` | RSS/API async fetch |
| `src/ai_processor.py` | LLM API call + prompt |
| `src/publisher.py` | Telegram send + MarkdownV2 escape |
| `src/main.py` | Bot, scheduler, handlers |
| `README.md` | Full documentation |
| `AGENTS.md` | Agent instructions |
| `DEPLOY.md` | Deployment guide |
| `tests/test_fetcher.py` | Fetcher unit tests |