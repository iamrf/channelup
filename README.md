# ChannelUp

RSS → LLM → Telegram **autoposter** built on an async producer–consumer pipeline.
Every feed runs on its own schedule with one of three modes — `raw` (no LLM),
`custom_llm` (per-feed prompt), or `curate` (accumulate, let the LLM pick, rewrite).
Publishing is throttled per channel to stay strictly under Telegram's 20 msgs/min
cap, and dedup + the curate accumulator live in Neon Postgres (asyncpg).

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env               # secrets: bot token, LLM key, Neon URL
cp channels.json.example channels.json   # your channels + feeds (modes, intervals)
python run_cron.py                # one-shot sweep of every feed
```

Always-on poller (admin commands + background producers/workers):

```bash
python -m channelup
```

## Configure

- **`.env`** — secrets + pipeline tuning (see `env.example`).
- **`channels.json`** — one entry per Telegram channel, each with a list of **feeds**.

```json
{
  "interval": 300,
  "telegram_rate_per_minute": 19,
  "curate_interval_seconds": 1800,
  "curate_batch_size": 10,
  "curate_top_n": 5,
  "channels": [
    {
      "name": "tech",
      "telegram_target": "-1001234567890",
      "language": "en",
      "feeds": [
        { "url": "https://www.theverge.com/rss/index.xml",
          "interval": 60,   "mode": "raw",
          "target_link": "https://t.me/tech_updates" },
        { "url": "https://feeds.arstechnica.com/arstechnica/index",
          "interval": 3600, "mode": "custom_llm",
          "custom_prompt": "Rewrite focusing on AI and developer-tooling. Write in {language}." },
        { "url": "https://feeds.bbci.co.uk/news/technology/rss.xml",
          "interval": 1800, "mode": "curate" }
      ]
    }
  ]
}
```

### Feed modes

| `mode` | Behavior |
|---|---|
| `raw` | **No LLM.** Posts the feed's own title/text verbatim, appending `target_link` (or the item URL). |
| `custom_llm` | Rewrites each item with the feed's `custom_prompt` (`{language}` substituted; falls back to the channel default). |
| `curate` | New items are stored in the DB. Every `curate_interval_seconds`, a batch of `curate_batch_size` is sent to the LLM, which picks the top `curate_top_n` most engaging, rewrites them, and publishes. |

### `.env` variables

| Var | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ADMIN_USER_IDS` | Comma-separated IDs allowed to run commands |
| `LLM_PROVIDER` | `gemini` \| `openai` \| `deepseek` \| `openrouter` |
| `LLM_API_KEY` | Key for the chosen provider |
| `LLM_MODEL` | e.g. `gemini-2.5-flash-lite`, `gpt-4o-mini`, `deepseek-chat` |
| `LLM_API_BASE_URL` | Optional. Empty = provider default. |
| `DATABASE_URL` | Neon PostgreSQL connection string (dedup + curate queue) |
| `LLM_CONCURRENCY` | Parallel LLM calls (default `4`) |
| `LLM_RATE_PER_MINUTE` | LLM token-bucket refill (default `60`) |
| `TELEGRAM_RATE_PER_MINUTE` | Global Telegram cap (default `19`, < 20) |
| `CONFIG_FILE` | Optional path override for `channels.json` |

## Commands (always-on mode, admins only)

- `/start` — config summary
- `/publish_now` — immediate one-shot sweep of all feeds
- `/status` — last run counters (fetched / rewritten / curated / published / errors)

## How it works

1. **Producers** — one task per (channel, feed), fetching on that feed's `interval`.
2. **Dedup** — `sha256(channel|link)` in the Neon `published` table
   (`INSERT … ON CONFLICT DO NOTHING`); an item is produced exactly once per channel.
3. **Routing by mode** — `raw` → publish queue directly; `custom_llm` → LLM queue;
   `curate` → `curate_items` table.
4. **Workers** — LLM workers rewrite (concurrency-capped + rate-limited), publish
   workers post. All rate limits are strict token buckets.
5. **Publishing** — `send_photo` when a lead image is available, else `send_message`.
6. A crashed feed/channel never aborts the others.

## Deploy & CI/CD

Two supported targets — **GitHub Actions (serverless) or Ubuntu self-host +
GitHub Actions CD** — plus the test workflow and a shared one-shot sweep runner.
See [DEPLOY.md](DEPLOY.md). **Enable only one scheduler.**

## Test

```bash
python -m pytest -q
```

## Deliberately skipped

- **Webhooks** — polling fits the queue pipeline; switch if you outgrow it.
- **Resharding queued items across nodes** — one process owns the pipeline; the
  curate queue makes it easy to shard later if needed.
- **DB pruning** — hashes are ~70 bytes; add a `DELETE WHERE ts < …` cron past a
  few million rows.
- **Media re-upload** — Telegram fetches image URLs directly; falls back to text.
- **Per-feed rate overrides** — Telegram cap is per channel; the LLM cap is global.