# AGENTS.md

Guidance for AI agents and humans working in this repo. Read this before changing
code. `README.md` covers usage/config; `DEPLOY.md` covers CI/CD; this file covers
structure, conventions, and gotchas.

## What this is

**ChannelUp** — a Telegram autoposter driven by an **async producer–consumer
pipeline**. Each configured feed has a `mode` (`raw` / `custom_llm` / `curate`)
and its own fetch `interval` (seconds). Items flow through `asyncio.Queue`s,
rewritten by the LLM, and posted under a **strict per-channel token-bucket** that
never exceeds Telegram's 20 msgs/min cap (default 19). Dedup + the `curate`
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

publish worker: acquire per-channel token bucket → publish (send_photo | send_message)
```

Key invariants — do not break them:
- **Rate limits are hard.** `ratelimit.TokenBucket` never sells a token it hasn't
  refilled; `pipeline` acquires **per Telegram channel** and for the LLM provider
  before every call. Default Telegram cap = `19/min` (safe under 20).
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
- **All I/O is async** via aiohttp / asyncpg; feed parsing runs in a thread
  (`asyncio.to_thread`) because feedparser is synchronous.

## File layout

| Path | Role |
|---|---|
| `channelup/config.py` | `Config` / `ChannelConfig` / `FeedConfig` (+`feed_prompt`) |
| `channelup/pipeline.py` | `Pipeline`: queues, producers, LLM/publish workers, curate, `sweep` |
| `channelup/db.py` | `Store` protocol + `PostgresStore` (asyncpg) + `MemoryStore` |
| `channelup/ratelimit.py` | `TokenBucket` + `RateLimiter` (per-key buckets) |
| `channelup/fetcher.py` | `clean`, `image_of`, `parse_source`, `fetch_sources` (pure) |
| `channelup/llm.py` | `_chat`, `rewrite`, `select_top` |
| `channelup/publisher.py` | `publish` to one target (`append_source` flag) |
| `channelup/bot.py` | `/start` `/publish_now` `/status`, `main` |
| `channelup/__main__.py` | `python -m channelup` → always-on pipeline |
| `run_cron.py` | one-shot `Pipeline.sweep` (GH cron + systemd timer) |
| `channels.json.example` / `env.example` | config templates |
| `deploy/setup.sh`, `deploy/channelup.service` | Ubuntu provisioning |
| `.github/workflows/{ci,deploy,cron}.yml` | tests, Ubuntu CD, serverless cron |
| `tests/` | pytest suite |

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
- **Adding a config field ⇒** update the matching dataclass(es), `from_dict`,
  `conftest.make_*` builders, and add a `test_config.py` case.
- **Adding a store method ⇒** implement it in BOTH `PostgresStore` and `MemoryStore`
  and the `Store` protocol; add a `test_db.py` (Memory) case.
- **Tests are hermetic.** `MemoryStore` replaces asyncpg; `FakeBot` + monkeypatched
  `channelup.pipeline.fetch_sources` / `rewrite` / `select_top` replace the network
  and LLM. `Pipeline` creates an aiohttp `ClientSession` in `__init__`, so build it
  **inside** `asyncio.run`/a coroutine in tests (never at module import time).
- **Use `asyncio.run` inside sync test functions** (no pytest-asyncio dependency).
- **`RateLimiter` keys are Telegram channel ids** — a per-channel bucket is
  created lazily with that channel's `rate_per_minute`.
- Anything new that talks to an external service must stay behind a seam (an async
  function / thread-`to_thread` boundary) so it can be stubbed.

## CI/CD

- `ci.yml` — tests on push/PR.
- `deploy.yml` — on `main`, tests → `rsync` to Ubuntu + `setup.sh` (systemd cron
  timer). Secrets: `DEPLOY_HOST` / `DEPLOY_USER` / `DEPLOY_SSH_KEY`.
- `cron.yml` — serverless one-shot `run_cron.py` on `*/30 * * * *` + manual dispatch.

> **Only one scheduler may be active.** The Ubuntu timer and `cron.yml` must not
> both run, or every item posts twice.