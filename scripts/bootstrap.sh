#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/prepare_config.py
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install --with-deps chromium
docker compose --env-file deployment/.env -f deployment/compose.yml up -d --build postgres api
install -m 644 deployment/codex-bridge.service /etc/systemd/system/tradingview-codex-bridge.service
systemctl daemon-reload
systemctl enable --now tradingview-codex-bridge.service
docker compose --env-file deployment/.env -f deployment/compose.yml --profile bots up -d worker trader analysis-bot trading-bot
