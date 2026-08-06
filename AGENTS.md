# AGENTS.md

Guidance for AI agents and humans working in this repo. Read this before changing code.

## What this is

**ChannelUp** — a Telegram autoposter pipeline: fetch RSS feeds → rewrite each item
with an LLM → post to one or more Telegram channels. Backed by Neon PostgreSQL for
deduplication. The core is a single file (`channelup.py`, ~330 lines).

`README.md` documents setup/config for users; this file documents structure and
conventions for people who edit the code.

## Architecture / data flow

```
RSS sources ──▶ feedparser ──▶ clean/format ──▶ dedup vs PostgreSQL ──▶ LLM rewrite ──▶ Telegram channels
                      (thread)     (module fns)   (published table)       (aiohttp)      (aiogram v3)
```

1. `fetch_all()` parses every `RSS_SOURCES` feed; `parse_feed()` returns
   `{title, link, text, image}` dicts. `clean()` strips HTML, `image_of()` sniffs
   media_content / enclosure / `<img>` for a lead image.
2. `fetch_and_filter()` runs in a worker thread; it queries the `published` table and
   returns only items whose `sha256(link)` hash is not yet recorded, capped at
   `MAX_ITEMS_PER_RUN`.
3. `rewrite()` sends `SYSTEM_PROMPT.format(language=...)` + the item to the provider
   (gemini / openai / deepseek / openrouter), retrying 3× with backoff on `429/5xx`.
4. `publish()` posts to every channel — `send_photo` when an image downloaded, else
   `send_message`, Telegram-HTML body with a `🔗 منبع خبر` link.
5. `mark()` records the hash **only after a successful post**, so failures retry next run.
6. `stats` dict tracks `last_run` / `published` / `errors` for the `/status` command.

## File layout

| File              | Role                                                           |
|-------------------|----------------------------------------------------------------|
| `channelup.py`    | The whole app: config, DB, fetch, LLM, publish, bot handlers.  |
| `prompts.py`      | `DEFAULT_PROMPT` — the default rewrite system prompt.          |
| `run_cron.py`     | One-shot runner for GitHub Actions / serverless cron (see `DEPLOY.md`). |
| `test_channelup.py` | Self-check script (see "Testing").                            |
| `env.example`     | Template for env config (copy to `.env`).                      |
| `requirements.txt`| Dependencies.                                                  |
| `DEPLOY.md`       | GitHub Actions + Neon deployment guide.                        |
| `channelup-plan.md` | **Stale** design notes from an earlier architecture — do not treat as source of truth. |

## Config & env

Config is read from environment at **import time** (module-level `os.environ` /
`os.getenv`, plus `load_dotenv()`). There is no config object or file — edits to
`.env` require a process restart.

Required (crash if missing): `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`, `TARGET_CHANNEL_IDS`,
`RSS_SOURCES`, `DATABASE_URL` (`sys.exit` if unset).

Optional with defaults: `LLM_PROVIDER` (`gemini`), `LLM_MODEL`, `LLM_API_BASE_URL`,
`PUBLISH_INTERVAL_MINUTES` (30), `MAX_ITEMS_PER_RUN` (5), `POST_DELAY_SECONDS` (5),
`LANGUAGE` (`fa`), `ADMIN_USER_IDS`, `SYSTEM_PROMPT` / `SYSTEM_PROMPT_FILE`.

## Commands

```bash
python channelup.py        # perpetual: polling bot + background loop
python run_cron.py         # one-shot: init DB, run once, exit (for CI/cron)
python test_channelup.py   # self-check: parsing, image extraction, dedup
```

## Conventions & gotchas

- **Import has side effects.** `channelup.py` module level connects to the DB (`init_db()`
  at import) and reads config. Tests and runners must set env vars *before* `import channelup`.
- **Blocking calls run in threads.** PostgreSQL (`fetch_and_filter`, `mark`) and the
  LLM/time work are wrapped in `asyncio.to_thread` / aiohttp — keep DB access blocked
  out of the event loop.
- **Dedup key is the item link** (`sha256(link)`), not the content — duplicate URLs
  across feeds collapse.
- **Telegram-HTML format only.** `DEFAULT_PROMPT` forbids block tags (`<p>`, `<br>`);
  `publish()` strips them defensively. Keep any prompt changes consistent with that.
- **Provider switch = env change only.** `LLM_PROVIDER` + `LLM_API_KEY` + `LLM_MODEL`,
  no code. Deliberately no per-channel prompts, webhooks, or DB pruning (see README
  "Deliberately skipped").

## Known issues

- **`test_channelup.py` is out of sync with `channelup.py`.** It references
  `c.seen`, `c.select_new`, and a `DB_PATH` env var that no longer exist (current code
  uses `DATABASE_URL`, `mark`, and `fetch_and_filter`), and it sets no `DATABASE_URL`,
  so it crashes on import. It needs updating alongside any real change to the dedup
  path; treat its asserts as intent, not as passing tests.