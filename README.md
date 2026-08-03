# ChannelUp

RSS → LLM rewrite → Telegram channel. One file, three dependencies.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env   # then edit .env
python channelup.py
```

## Configure (`.env`)

| Var | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TARGET_CHANNEL_IDS` | Comma-separated. `-100...` numeric ID or `@public_name`. Add bot as channel admin with *Post Messages*. |
| `ADMIN_USER_IDS` | Comma-separated user IDs allowed to run commands ([@userinfobot](https://t.me/userinfobot)) |
| `LLM_PROVIDER` | `gemini` \| `openai` \| `deepseek` \| `openrouter` |
| `LLM_API_KEY` | Key for the chosen provider |
| `LLM_MODEL` | e.g. `gemini-2.0-flash`, `gpt-4o-mini`, `deepseek-chat`, `openai/gpt-4o-mini` |
| `LLM_API_BASE_URL` | Optional. Empty = provider default. |
| `RSS_SOURCES` | Comma-separated feed URLs |
| `PUBLISH_INTERVAL_MINUTES` | Poll interval. `1` ≈ real-time. |
| `MAX_ITEMS_PER_RUN` | Flood guard |
| `POST_DELAY_SECONDS` | Gap between posts |
| `LANGUAGE` | Output language, substituted into the prompt |
| `SYSTEM_PROMPT` / `SYSTEM_PROMPT_FILE` | Override the default rewrite prompt. `{language}` is substituted. |

### API keys

- Gemini — <https://aistudio.google.com/apikey>
- OpenAI — <https://platform.openai.com/api-keys>
- DeepSeek — <https://platform.deepseek.com/api_keys>
- OpenRouter — <https://openrouter.ai/keys>

Switching provider = change `LLM_PROVIDER` + `LLM_API_KEY` + `LLM_MODEL`, restart. No code change.

## Commands (admins only)

- `/start` — config summary
- `/publish_now` — immediate run
- `/status` — last run, totals, error count

## How it works

1. Loop every `PUBLISH_INTERVAL_MINUTES` (plus `/publish_now` on demand).
2. `feedparser` reads each feed; items already in the SQLite `published` table are skipped.
3. Title + body + link go to the LLM with the system prompt; retries 3× with backoff on 429/5xx.
4. Result posts to every channel — `send_photo` when the feed has an image, else `send_message`.
5. Item hash is recorded **only after a successful post**, so failures retry next run.

## Run as a service

```ini
# /etc/systemd/system/channelup.service
[Unit]
Description=ChannelUp
After=network-online.target

[Service]
WorkingDirectory=/opt/channelup
ExecStart=/opt/channelup/.venv/bin/python channelup.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now channelup
journalctl -u channelup -f
```

## Deliberately skipped

- **Webhooks** — polling is fine for one bot; switch if you outgrow it.
- **Per-channel prompts/schedules** — one config set. Add a channels table when you need a second editorial voice.
- **DB pruning** — hashes are ~70 bytes; add a `DELETE WHERE ts < …` cron past a few million rows.
- **Media re-upload** — Telegram fetches image URLs directly; falls back to text if the host blocks it.

## Test

```bash
python test_channelup.py
```