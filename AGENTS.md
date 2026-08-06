# AGENTS.md

Guidance for AI agents and humans working in this repo. Read this before changing
code. `README.md` covers usage/config; `DEPLOY.md` covers CI/CD and deployment;
this file covers structure, conventions, and gotchas for editors.

## What this is

**ChannelUp** — a Telegram autoposter: on a schedule (or `/publish_now`), fetch
each configured channel's RSS sources, rewrite new items with an LLM, and post
them. Each Telegram channel is independently configured with `rss_sources`,
an optional `prompt_addon` (layered on the shared default prompt), and per-channel
tuning. Dedup is per `(channel, link)` against Neon PostgreSQL.

## Architecture / data flow

```
each channel in channels.json
  └─ fetch_sources(rss) ─▶ select_new(store) ─▶ rewrite(LLM, per-channel prompt) ─▶ publish ─▶ store.mark
       (thread)               (dedup, capped)      (aiohttp, backoff)                (aiogram)   (only after success)
```

Key invariants, do not break them:
- **Mark only after a successful post** — an item that fails is retried next run.
- **Dedup key = `sha256(f"{channel}|{link}")`** — the same RSS link can reach
  different channels independently.
- **Blocking work runs in threads** — PostgreSQL (`store`) and feed parsing are
  invoked via `asyncio.to_thread`; never hold the event loop with a DB call.
- **Per-channel prompt = `prompt_addon` prepended to `DEFAULT_PROMPT`, `{language}`
  substituted per channel** (see `config.build_system_prompt`).
- **Telegram-HTML only.** The default prompt forbids block tags; `publisher` strips
  them defensively. Keep prompt/HTML changes consistent with that.
- **One channel crash never aborts the others** (`runner.run_channels`).

## File layout

| Path | Role |
|---|---|
| `channelup/__init__.py` | version |
| `channelup/config.py` | `Config` / `ChannelConfig` from `channels.json` + env; `build_system_prompt` |
| `channelup/prompts.py` | `DEFAULT_PROMPT` |
| `channelup/db.py` | `DedupStore` protocol + `PostgresDedupStore` + `MemoryDedupStore` |
| `channelup/fetcher.py` | `clean`, `image_of`, `parse_source`, `fetch_sources` (pure) |
| `channelup/llm.py` | `rewrite` provider call with 3× backoff |
| `channelup/publisher.py` | `publish` to one `telegram_target` |
| `channelup/runner.py` | `select_new`, `run_channel`, `run_channels`, `stats` |
| `channelup/bot.py` | `/start` `/publish_now` `/status`, background loop, `main` |
| `channelup/__main__.py` | `python -m channelup` → always-on polling |
| `run_cron.py` | one-shot: process every channel, exit (GH cron + systemd timer) |
| `channels.json.example` | template for `channels.json` (gitignored) |
| `env.example` | secrets template (`.env` is gitignored) |
| `deploy/setup.sh`, `deploy/channelup.service` | Ubuntu provisioning |
| `.github/workflows/{ci,deploy,cron}.yml` | tests, Ubuntu CD, serverless cron |
| `tests/` | pytest suite |

## Commands

```bash
.venv/bin/python -m pytest -q    # tests (no DB/network/Telegram needed)
.venv/bin/python run_cron.py     # one-shot cron run (needs .env + channels.json)
.venv/bin/python -m channelup    # always-on polling bot
```

## Conventions & gotchas

- **Config = secrets (env) + channel defs (`channels.json`).** `channels.json` is
  non-secret and **committed** (the serverless CI job runs against the checked-out
  copy); the Ubuntu deploy `rsync`s it out so `setup.sh` keeps the server's own
  local file. Global defaults (`language`, `max_items_per_run`, `post_delay_seconds`)
  sit at the file top level; per-channel entries override them.
- **Do not add schema-specific env vars** like `RSS_SOURCES`/`TARGET_CHANNEL_IDS`
  /`SYSTEM_PROMPT` from the old single-channel design — those are gone.
- **Add a `ChannelConfig` field ⇒** update `from_dict`, `build_system_prompt` (if
  prompt-related), the `conftest.make_config` helper, and a `tests/test_config.py`
  case. The `DedupStore` protocol takes `(channel, link)` — new lookup types go there.
- **Tests are hermetic.** `MemoryDedupStore` replaces Postgres; the LLM/network and
  Telegram layers are stubbed (see `tests/test_runner.py`'s `FakeBot` and
  monkeypatched `channelup.runner.rewrite`). Postgres SQL is asserted without a live
  DB in `tests/test_db.py` via a fake connection. Run `pytest` from the repo root.
- **Anything new that talks to an external service** must stay behind the same seams
  (an async function / a thread-bound sync call) so it can be stubbed in tests.

## CI/CD

- `ci.yml` — tests on push/PR.
- `deploy.yml` — on `main`, run tests then `rsync` to the Ubuntu host + `setup.sh`
  (systemd cron timer). Reads `DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_SSH_KEY` secrets.
- `cron.yml` — serverless one-shot, runs on `*/30 * * * *` and manual dispatch
  (disable it if you add the Ubuntu timer to avoid double-posting).

> **Only one scheduler may be active.** The Ubuntu timer (from `setup.sh`) and the
> `cron.yml` serverless job must not both run, or every item posts twice.