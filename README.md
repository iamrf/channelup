# ChannelUp

RSS → LLM rewrite → Telegram channel **autoposter**. One small Python package,
three dependencies. Each Telegram channel gets its **own RSS sources** and an
**optional prompt add-on** layered on top of a shared default editorial prompt.
Dedup is per channel, backed by Neon PostgreSQL.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env               # edit: secrets (bot token, LLM key, Neon URL)
cp channels.json.example channels.json   # edit: your channels, sources, prompts
python run_cron.py                # one-shot run of every channel
```

For the always-on polling mode (admin commands + background loop) instead:

```bash
python -m channelup
```

## Configure

Two files, split by sensitivity:

- **`.env`** — secrets. See `env.example`.
- **`channels.json`** — one entry per Telegram channel (non-secret).

```json
{
  "language": "fa",            // global defaults
  "max_items_per_run": 5,
  "post_delay_seconds": 5,
  "channels": [
    {
      "name": "tech",                          // unique ID
      "telegram_target": "-1001234567890",      // numeric -100… or @public_name
      "rss_sources": [
        "https://www.theverge.com/rss/index.xml"
      ],
      "prompt_addon": "Coverage should emphasize AI and developer-tooling angles.",
      "language": "en",                          // optional per-channel override
      "max_items_per_run": 3
    }
  ]
}
```

The `prompt_addon` is prepended to the shared `DEFAULT_PROMPT` (`channelup/prompts.py`);
`{language}` is substituted per channel. Omit it to use the default prompt as-is.

### `.env` variables

| Var | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ADMIN_USER_IDS` | Comma-separated IDs allowed to run commands |
| `LLM_PROVIDER` | `gemini` \| `openai` \| `deepseek` \| `openrouter` |
| `LLM_API_KEY` | Key for the chosen provider |
| `LLM_MODEL` | e.g. `gemini-2.0-flash`, `gpt-4o-mini`, `deepseek-chat` |
| `LLM_API_BASE_URL` | Optional. Empty = provider default. |
| `DATABASE_URL` | Neon PostgreSQL connection string (dedup store) |
| `PUBLISH_INTERVAL_MINUTES` | Poll interval for the always-on mode |
| `CONFIG_FILE` | Optional path override for `channels.json` |

Switching provider = change `LLM_PROVIDER` + `LLM_API_KEY` + `LLM_MODEL`, restart. No code change.

## Commands (always-on mode, admins only)

- `/start` — config summary
- `/publish_now` — immediate run of all channels
- `/status` — last run, totals, error count

## How it works

1. On schedule (or `/publish_now`), each channel in `channels.json` is processed:
   its own `rss_sources` are fetched by `feedparser`.
2. Items already in the Neon `published` table (keyed per `channel` + link) are skipped.
3. Title + body + link go to the LLM with the channel's system prompt
   (`prompt_addon` + default); retries 3× with backoff on 429/5xx.
4. Result posts to the channel's `telegram_target` — `send_photo` when a lead
   image is available, else `send_message`.
5. The item hash is recorded **only after a successful post**, so failures retry next run.
6. One channel crash never aborts the others.

## Deploy & CI/CD

Two supported targets — **GitHub Actions (serverless) or Ubuntu self-host +
GitHub Actions CD** — plus the test workflow and a shared one-shot cron runner.
See [DEPLOY.md](DEPLOY.md). **Enable only one scheduler.**

## Test 

```bash
python -m pytest -q
```

## Deliberately skipped

- **Webhooks** — polling is fine for one bot; switch if you outgrow it.
- **Per-channel DB hooks / complex scheduling** — one cron cadence, per-channel
  tuning via `channels.json`. Add a scheduler config when you need independent timing.
- **DB pruning** — hashes are ~70 bytes; add a `DELETE WHERE ts < …` cron past a few million rows.
- **Media re-upload** — Telegram fetches image URLs directly; falls back to text if the host blocks it.
