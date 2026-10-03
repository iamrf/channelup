# AGENTS.md

Guidance for AI agents and humans working in this repo. Read this before changing
code. `README.md` covers usage/config; `DEPLOY.md` covers CI/CD; this file covers
structure, conventions, and gotchas.

## What this is

**ChannelUp** — a Telegram autoposter driven by an **async producer–consumer
pipeline**. Each configured feed has a `mode` (`raw` / `custom_llm` / `curate`)
and its own fetch `interval` (seconds). Items flow through `asyncio.Queue`s,
rewritten by the LLM, and posted under a **strict per-target token-bucket** that
never exceeds Telegram's 20 msgs/min cap (default 19). A `telegram_target` may be
a **channel or group/supergroup** (`@name` or `-100…` id). Dedup + the `curate`
accumulator live in Neon Postgres via asyncpg.

## Architecture

```text
per (channel, feed) producer task, every feed.interval
   └─ fetch_sources ─▶ try_mark_seen (dedup, atomic) ─▶ route by mode:
        raw        ────────────────────────────────▶ publish_queue
        custom_llm ─▶ llm_queue ─▶ LLM worker ─────▶ publish_queue
        curate     ─▶ store.enqueue_curate ─┐
                                            ▼
        curate job (every curate_interval): claim batch ─▶ LLM select_top ─▶ rewrite ─▶ publish_queue

publish worker: acquire N tokens (1, or 2 for photo+long caption) per telegram_target
             → publish (send_photo | send_message; HTML sanitize + plain fallback)
```

Key invariants — do not break them:
- **Rate limits are hard.** `ratelimit.TokenBucket` never sells a token it hasn't
  refilled; `pipeline` acquires **per `telegram_target`** (and for the LLM
  provider) before every call. Default Telegram cap = `19/min` (safe under 20).
  Long captions that become photo + follow-up message reserve **two** tokens.
- **Dedup happens at production** (`try_mark_seen`, atomic `INSERT … ON CONFLICT`).
  An item is produced exactly once per channel; failures within the pipeline are
  logged and counted, not retried (retry of transient LLM errors is inside `llm._chat`,
  3× backoff on 429/5xx).
- **raw never touches the LLM.** It copies the feed title/text and appends the
  feed's `target_link`.
- **Custom prompts are layered:** `feed.custom_prompt ⊃ channel.channel_prompt ⊃
  DEFAULT_PROMPT` (empty layers skipped, `{language}` resolved; `config.feed_prompt`).
- **curate pools per channel.** All `curate` feeds of one channel accumulate into a
  single shared pool; each schedule tick claims `curate_batch_size` across all of
  them, the LLM picks `curate_top_n` via `llm.select_top` (falls back to first-N if
  its JSON can't be mapped back to candidate URLs), and the winners are rewritten
  and published.
- **Publish is defensive.** `publisher.publish` sanitizes HTML to Telegram's
  allowlist, HTML-escapes source hrefs, downloads+re-uploads lead images, falls
  back to text on photo failure, and retries as plain text when entity parsing
  fails. Fatal chat errors (`chat not found`, not a member, no rights) propagate.
- **All I/O is async** via aiohttp / asyncpg; feed parsing runs in a thread
  (`asyncio.to_thread`) because feedparser is synchronous.
- **Neon is serverless:** its compute can sleep, dropping a pooled connection
  mid-query. `PostgresStore` retries transient connection errors
  (`ConnectionDoesNotExistError`, `ConnectionFailureError`, …) by re-acquiring
  from the pool, and `curate_items` has `UNIQUE (channel, link)` so a retried
  enqueue can never double-queue an item.

## File layout

| Path | Role |
|---|---|
| `channelup/config.py` | `Config` / `ChannelConfig` / `FeedConfig` (+`feed_prompt`) |
| `channelup/pipeline.py` | `Pipeline`: queues, producers, LLM/publish workers, curate, `sweep` |
| `channelup/db.py` | `Store` protocol + `PostgresStore` (asyncpg) + `MemoryStore` |
| `channelup/ratelimit.py` | `TokenBucket` + `RateLimiter` (per-key buckets) |
| `channelup/fetcher.py` | `clean`, `image_of`, `parse_source`, `fetch_sources` (pure) |
| `channelup/llm.py` | `_chat`, `rewrite`, `select_top` |
| `channelup/publisher.py` | `publish` / `prepare_body` / `message_slots` (channel or group) |
| `channelup/bot.py` | `/start` `/publish_now` `/status`, startup target checks, `main` |
| `channelup/__main__.py` | `python -m channelup` → always-on pipeline |
| `run_cron.py` | one-shot `Pipeline.sweep` (GH cron + systemd timer) |
| `channels.json.example` / `env.example` | config templates |
| `deploy/setup.sh`, `deploy/channelup.service` | Ubuntu provisioning |
| `deploy/render_server.py`, `render.yaml` | Render Web Service entry + Blueprint |
| `.github/workflows/{ci,deploy,cron}.yml` | tests, Ubuntu CD, serverless cron |
| `tests/` | pytest suite (`test_publisher.py` covers publish paths) |

## Commands

```bash
.venv/bin/python -m pytest -q    # tests (hermetic: no DB/network/Telegram)
.venv/bin/python run_cron.py     # one-shot sweep (needs .env + channels.json)
.venv/bin/python -m channelup    # always-on pipeline
```

## Conventions & gotchas

- **Config = secrets (env) + feed defs (`channels.json`).** `channels.json` is
  non-secret and committed (serverless CI runs against the checkout); the Ubuntu
  deploy `rsync`s it out so the server keeps its own copy. Feeds are per channel:
  `url`, `interval` (seconds), `mode`, optional `custom_prompt` / `target_link`.
  The loader (`config.load_json`) is JSONC-tolerant: `//` and `/* */` comments and
  trailing commas are allowed, so the `.example` file is fully commented.
- **`telegram_target` is a channel or group.** Channels need the bot as admin
  with Post Messages; groups need membership (admin if posting is restricted).
  Startup verifies every target via `get_chat`.
- **Adding a config field ⇒** update the matching dataclass(es), `from_dict`,
  `conftest.make_*` builders, and add a `test_config.py` case.
- **Adding a store method ⇒** implement it in BOTH `PostgresStore` and `MemoryStore`
  and the `Store` protocol; add a `test_db.py` (Memory) case.
- **Tests are hermetic.** `MemoryStore` replaces asyncpg; `FakeBot` + monkeypatched
  `channelup.pipeline.fetch_sources` / `rewrite` / `select_top` replace the network
  and LLM. Publisher tests use a local `FakeBot`/`FakeSession` in
  `tests/test_publisher.py`. `Pipeline` creates an aiohttp `ClientSession` in
  `__init__`, so build it **inside** `asyncio.run`/a coroutine in tests (never at
  module import time).
- **Use `asyncio.run` inside sync test functions** (no pytest-asyncio dependency).
- **`RateLimiter` keys are `telegram_target` strings** — a per-chat bucket is
  created lazily with that channel's `rate_per_minute`.
- Anything new that talks to an external service must stay behind a seam (an async
  function / thread-`to_thread` boundary) so it can be stubbed.

## CI/CD

- `ci.yml` — tests on push/PR.
- `deploy.yml` — on `main`, tests → `rsync` to Ubuntu + `setup.sh` (systemd cron
  timer). Secrets: `DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_SSH_KEY`.
- `cron.yml` — serverless one-shot `run_cron.py` on `*/30 * * * *` + manual dispatch.
- **Render** — *not* a workflow; it's a manual Web Service deploy via
  `deploy/render_server.py` + `render.yaml`. See DEPLOY.md Option C.

> Deploy to **one** target. Running any two schedulers (cron.yml, the Ubuntu timer,
> or the Render service) at once double-posts every item.
