# ChannelUp — Deployment & CI/CD

ChannelUp re-casts your RSS feeds into Telegram posts daily via an LLM. It runs a
**cron** job — fetch feeds → deduplicate → rewrite → post — and is deployed through
GitHub Actions. This document covers both supported runtime targets.

## Two ways to schedule (pick ONE)

| | Serverless (GitHub Actions) | Ubuntu self-host + CI/CD |
|---|---|---|
| Runs on | a throwaway GitHub-hosted runner | your own Ubuntu box |
| Scheduler | `cron.yml` on a schedule | `deploy/setup.sh` → systemd timer |
| Config | secrets in GitHub + `channels.json` in repo | `.env` + `channels.json` on the box |
| Commands `/publish_now` | no | yes (`channelup.service`) |
| CI on push | partially | fully (tests + auto-deploy) |

> **Do not enable both** — you would double-post. Pick serverless **or** the
> Ubuntu timer/service and disable the other. If you add the Ubuntu box and its
> systemd timer later, stop `cron.yml` (or remove it) first.

## Code layout

```
channelup/
  __init__.py    __main__.py      # python -m channelup  (always-on pipeline)
  config.py      # Config + ChannelConfig + FeedConfig from channels.json + env
  prompts.py     # DEFAULT_PROMPT
  db.py          # Store (asyncpg/Neon): dedup + curate queue (+ MemoryStore for tests)
  ratelimit.py   # TokenBucket / RateLimiter (Telegram + LLM caps)
  fetcher.py     # RSS parse / clean / image
  llm.py         # _chat / rewrite / select_top (curate)
  publisher.py   # post to one Telegram channel
  pipeline.py    # Producer-Consumer: queues, workers, curate, sweep
  bot.py         # /start /publish_now /status + pipeline start
run_cron.py      # one-shot: Pipeline.sweep over every feed once, exit
channels.json.example
deploy/
  setup.sh                # server bootstrap (venv + systemd timer)
  channelup.service       # OPTIONAL always-on polling unit
.github/workflows/
  ci.yml                  # tests on push/PR
  deploy.yml              # CD: run tests, sync repo, call setup.sh
  cron.yml                # serverless scheduled cron
```

## Configuration

- **`channels.json`** — non-secret, one entry per Telegram channel, each with a
  list of `feeds`: `url`, `interval` (seconds), `mode` (`raw` | `custom_llm` |
  `curate`), optional `custom_prompt` / `target_link`. Global defaults
  (`interval`, `language`, `telegram_rate_per_minute`, `curate_*`) can sit at the
  top of the file. Copy from `channels.json.example`.
- **`.env`** (Ubuntu) or **GitHub Secrets** (serverless) — secrets:
  `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `LLM_API_KEY` (+ `LLM_PROVIDER` /
  `LLM_MODEL` / optional tuning vars). See `env.example`.

---

## Option A — Ubuntu self-host + CD

1. **One-time server bootstrap**
   ```bash
   sudo useradd -m -U channelup    # (optional) dedicated user
   sudo mkdir -p /opt/channelup && sudo chown -R $(whoami) /opt/channelup
   # scp/rsync the repo, or just run the workflow once (it creates /opt/channelup)
   cp env.example /opt/channelup/.env        # fill in secrets
   cp channels.json.example /opt/channelup/channels.json   # edit your channels
   APP_DIR=/opt/channelup APP_USER=$USER bash /opt/channelup/deploy/setup.sh
   ```
   This creates a venv and installs a **systemd timer** that runs `run_cron.py`
   every 30 min (`OnUnitActiveSec=30m` — edit in `setup.sh`).

2. **Add GitHub secrets** (Settings → Secrets and variables → Actions):
   `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` (the private key for a user that
   owns `/opt/channelup` and can `sudo systemctl`). The integrated deploy key or a
   dedicated deploy-only key both work.

3. Push to `main`. `.github/workflows/deploy.yml`:
   runs `pytest` (gate) → `rsync`s the repo to `DEPLOY_HOST:/opt/channelup`
   (excluding `.git`, `.venv`, `.env`, `channels.json`, caches) → runs `setup.sh`
   to refresh the venv and reinstall/enable the timer.

   Inspect runs: `journalctl -u channelup-cron.timer -f` or `systemctl list-timers | grep channelup`.

4. **(Optional) Always-on bot** with `/publish_now` and `/status`: install
   `deploy/channelup.service` instead of the timer (see comments in that file).
   Remember: use the **service** or the **timer**, not both.

## Option B — Serverless (GitHub Actions only)

1. Add **GitHub secrets**: `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `LLM_API_KEY`,
   `LLM_PROVIDER`, `LLM_MODEL` (and `LLM_API_BASE_URL` if you use one).
2. Commit a real `channels.json` to the repo (it's non-secret) — `cron.yml` runs
   against the checked-out copy.
3. `cron.yml` runs on schedule and on manual dispatch — no extra toggle needed.
4. Manually verify once from the Actions tab (`Run workflow` on `cron.yml`), then
   let the `*/30 * * * *` schedule take over.

No server, no deploy step — but there is no `/publish_now` and each run is billed
to GitHub Actions minutes.

---

## CI

`.github/workflows/ci.yml` runs `python -m pytest -q` on push and pull requests for
every branch. The deploy job re-runs the same gate before syncing to the server.

## Manual / local run

```bash
cp channels.json.example channels.json   # edit
cp env.example .env                       # fill secrets
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# one-shot (same as the cron)
.venv/bin/python run_cron.py

# always-on polling bot (long-running)
.venv/bin/python -m channelup
```

## Troubleshooting

- **Nothing posts** → check `.env`/secrets, `channels.json` exists, and the bot is
  admin on each `telegram_target` with *Post Messages* rights. `-100…` numeric IDs
  must include the `-100` prefix.
- **Double posts** → both schedulers are active. Disable one (see top).
- **Deploy fails at SSH** → `DEPLOY_HOST` reachable? key in `DEPLOY_SSH_KEY` carried
  by the right user? does that user own `/opt/channelup` and have `sudo`?