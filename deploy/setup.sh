#!/usr/bin/env bash
# Idempotent server provisioning for ChannelUp (run by the GitHub Actions deploy
# workflow over SSH, or by hand). Installs the venv and a systemd one-shot timer
# that runs `run_cron.py` on your schedule.
#
# Usage:
#   bash /opt/channelup/deploy/setup.sh [silent]
# Environment (defaults shown):
#   APP_DIR=/opt/channelup   APP_USER=$USER   CRON_SCHEDULE=OnBootSec=60;OnUnitActiveSec=30m
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
APP_USER="${APP_USER:-$USER}"
SILENT="${1:-}"

say() { if [ "$SILENT" != "silent" ]; then printf '%s\n' "$*"; else echo "[setup] $*"; fi; }

cd "$APP_DIR"

# 1. Python virtualenv + dependencies
say "Setting up venv at $APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# 2. Warn if .env / channels.json are missing (first deploy)
[ -f "$APP_DIR/.env" ]        || say "WARNING: $APP_DIR/.env missing — copy env.example and fill secrets."
[ -f "$APP_DIR/channels.json" ] || say "WARNING: $APP_DIR/channels.json missing — copy channels.json.example and edit."

# 3. systemd cron timer -> one-shot run of run_cron.py
say "Installing channelup-cron.service / .timer"
sudo tee /etc/systemd/system/channelup-cron.service >/dev/null <<EOF
[Unit]
Description=ChannelUp one-shot channel run

[Service]
Type=oneshot
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run_cron.py
EOF

sudo tee /etc/systemd/system/channelup-cron.timer >/dev/null <<EOF
[Unit]
Description=Run ChannelUp on schedule

[Timer]
OnBootSec=60
OnUnitActiveSec=30m
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now channelup-cron.timer
say "channelup-cron.timer enabled — next run within ~30m of boot/last run."

# Optional companion: the always-on polling bot ("python -m channelup") for the
# /start /publish_now /status commands. Only enable_one_of_the_two:
#
#   sudo systemctl enable --now channelup.service
#   sudo systemctl stop --now channelup-cron.timer   # don't run BOTH schedulers
say "Done. View logs with: journalctl -u channelup-cron.timer -f"