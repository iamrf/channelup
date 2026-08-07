# ChannelUp — Deployment & CI/CD

ChannelUp posts Telegram messages from RSS feeds through an LLM. It can run three
ways. **Pick exactly ONE scheduler** — running two posts every item twice.

| | A. GitHub Actions (serverless) | B. Ubuntu self-host + CI/CD | C. Render Web Service (manual) |
|---|---|---|---|
| Runs on | a throwaway GitHub-hosted runner | your own Ubuntu box | Render's cloud (always-on) |
| Type | scheduled cron (`*/30 * * * *`) | systemd timer (auto-deployed) | long-running web service |
| Auto-deploy on push | no | **yes** (GH CD → SSH) | **yes*** (Render auto-deploy) |
| Admin `/publish_now` | no | yes | yes |
| Cost | GH Actions minutes | your VPS | Render free/paid |
| Effort | secrets only | server + secrets | dashboard clicks |

\* Render auto-deploys from the connected repo; the deploy *itself* is configured
manually once.

---

## Config (shared by all options)

- **`channels.json`** — non-secret; one entry per Telegram channel with a `feeds`
  list (`url`, `interval`, `mode` = `raw` | `custom_llm` | `curate`, optional
  `target_link` / `custom_prompt`). JSONC comments/trailing commas allowed. It is
  committed, so every deploy uses the same copy.
- **`.env` / secrets** — `TELEGRAM_BOT_TOKEN`, `DATABASE_URL` (Neon), `LLM_API_KEY`
  (+ `LLM_PROVIDER`, `LLM_MODEL`, optional tuning). See `env.example`.

> `DATABASE_URL` must be **Neon** (or any externally-accessible Postgres) in every
> target — serverless runners, VMs and Render all have **ephemeral** storage, so a
> local Postgres would lose your dedup + curate queue on every restart.

---

## Option A — GitHub Actions (serverless)

1. Commit a real `channels.json` to the repo.
2. Add **GitHub secrets**: `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`, `LLM_API_KEY`,
   `LLM_PROVIDER`, `LLM_MODEL` (and `LLM_API_BASE_URL` if used).
3. `.github/workflows/cron.yml` runs `run_cron.py` every 30 min and on manual
   dispatch. Concurrency is guarded so schedule + manual can't overlap.
4. Verify once from the Actions tab (`Run workflow`).

Pros: zero servers. Cons: no `/publish_now`, billed GH minutes.

---

## Option B — Ubuntu self-host + GitHub Actions CI/CD (auto-deploy)

Runs on your Ubuntu VPS. Every push to `main` runs the tests, then deploys the
code and reloads the systemd cron timer automatically.

### One-time server bootstrap

```bash
sudo useradd -m -U channelup          # optional dedicated user
sudo mkdir -p /opt/channelup && sudo chown -R $USER /opt/channelup
# first copy of the repo (later deploys come from the workflow):
git clone https://github.com/<you>/channelup /opt/channelup   # or rsync
cp env.example /opt/channelup/.env                # fill secrets
cp channels.json.example /opt/channelup/channels.json   # edit feeds
APP_DIR=/opt/channelup APP_USER=$USER bash /opt/channelup/deploy/setup.sh
```

`setup.sh` creates the venv, installs deps and installs/enables a **systemd timer**
that runs `run_cron.py` every 30 min (`OnUnitActiveSec=30m` — edit in the script).
It warns if `.env` / `channels.json` are missing. Logs:
`journalctl -u channelup-cron.timer -f`.

### GitHub secrets for auto-deploy

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | VPS IP/hostname, e.g. `1.2.3.4` |
| `DEPLOY_USER` | SSH user owning `/opt/channelup` with `sudo` (e.g. `ubuntu`) |
| `DEPLOY_SSH_KEY` | private key (ed25519) the runner uses to SSH in |

### What `.github/workflows/deploy.yml` does (on push to `main`)

1. Runs `pytest` (test gate).
2. `rsync`s the repo to `DEPLOY_HOST:/opt/channelup`, excluding `.git`, `.venv`,
   `.env`, `channels.json`, and caches (`--delete` keeps those excluded files).
3. SSHes in and runs `deploy/setup.sh silent` — refresh venv, reinstall/enable the
   timer.
4. A `concurrency` guard cancels an in-flight deploy if a newer commit lands.

### Optional: always-on bot instead of the timer

If you want `/publish_now` + `/status` on the VPS, install `deploy/channelup.service`
(the always-on polling bot) instead of the timer — **not both**. See comments in
that file.

> **Do not** run the Ubuntu timer AND `cron.yml` (Option A) at the same time.

---

## Option C — Render Web Service (manual, full guide)

Render hosts the always-on bot. Because it's a **Web Service**, we bind a tiny
health endpoint on `$PORT` (so Render marks the deploy healthy) alongside the
pipeline. See `deploy/render_server.py`.

### Step 1 — prerequisites

- A Neon `DATABASE_URL` (Render's filesystem is ephemeral).
- `channels.json` committed to the repo (already the case).
- `render.yaml` committed (already the case) — the optional blueprint.

### Step 2 — create the Web Service (two ways)

**Blueprint (fastest):** Render → **New** → **Blueprint** → select this repo →
publish. It reads `render.yaml` and pre-fills everything; you then fill the
`sync: false` secrets in the Environment tab and click **Deploy**.

**Manual (click-by-click):**
1. **New** → **Web Service**.
2. **Connect** the GitHub repo (Render must be authorized; it auto-deploys on push).
3. **Name**: `channelup`. **Region**: nearest to you.
4. **Root directory**: `/`.
5. **Runtime**: `Python 3`.
6. **Build Command**: `pip install -r requirements.txt` *(Reads `requirements.txt`
   automatically, but set it explicitly if you change it).*
7. **Start Command**: `python deploy/render_server.py`.
8. **Instance Type**: Free (or paid; Free spins down after ~15 min idle — see note
   below).
9. Click **Create Web Service**.

> The `PORT` env var is set by Render automatically; `deploy/render_server.py`
> binds the health server to it. No need to set it.

### Step 3 — environment variables

In the **Environment** tab add:

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `DATABASE_URL` | your Neon connection string |
| `LLM_API_KEY` | provider key |
| `LLM_PROVIDER` | `gemini` (or `openai` / …) |
| `LLM_MODEL` | `gemini-2.5-flash-lite` |
| `ADMIN_USER_IDS` | comma-separated admin Telegram IDs |
| `CONFIG_FILE` | `channels.json` (default) |

Render auto-provides `PORT`. Save & deploy (or Render redeploys automatically).

### Step 4 — verify

- **Health check**: Render pings `/health` — the service shows **Live**.
- **Logs**: Render → Logs tab; expect `Channel verified: …`, `health server
  listening`, and pipeline counters.
- **Telegram**: check the target channel; `/status` works if `ADMIN_USER_IDS` is set.

### Notes & limits

- **Ephemeral filesystem** — never store anything locally; only Neon is durable.
- **Free tier spins down** after ~15 min without traffic. Since the bot only sends
  (no inbound web traffic to wake it), a free Render Web Service may sleep and skip
  runs. For reliable 24/7, use a **paid Starter/Pro** service or keep the bot
  polling (a polling bot generates no inbound HTTP, so free tier sleep risk applies).
- **No `render.yaml` required** — it's a convenience; the manual steps above are the
  source of truth.
- Render auto-deploys every push to the connected branch. To pause, disable
  **Auto-Deploy** on the service.

> Running Option C and anything else (A or B) simultaneously will **double-post**.

---

## CI

`.github/workflows/ci.yml` runs `pytest` on push + pull requests. The Ubuntu deploy
re-runs the same gate before syncing (deploy.yml) — a red test blocks the deploy.

## Manual / local run

```bash
cp channels.json.example channels.json   # edit
cp env.example .env                       # fill secrets
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# one-shot (same as GH cron / systemd timer)
.venv/bin/python run_cron.py

# always-on pipeline + bot
.venv/bin/python -m channelup
```

## Troubleshooting

- **Nothing posts** → `.env`/secrets present? `channels.json` exists? Bot is admin
  on every `telegram_target` with *Post Messages*? `-100…` IDs need the `-100`.
- **`column "mode" does not exist`** → the Neon DB predates the new schema; the app
  now auto-migrates on boot (`ALTER TABLE … ADD COLUMN IF NOT EXISTS`). Restart once.
- **`connection was closed in the middle of operation`** → Neon serverless dropped
  an idle connection; the store retries automatically. If it persists, check
  `DATABASE_URL` and Neon compute status.
- **Double posts** → two schedulers active. Disable one (see the top table).
- **Deploy fails at SSH** → `DEPLOY_HOST` reachable? key in `DEPLOY_SSH_KEY` owned by
  `DEPLOY_USER`? does that user own `/opt/channelup` and have `sudo`?
- **Render unhealthy** → confirm start command is `python deploy/render_server.py`
  and it can reach Neon (`DATABASE_URL` set).