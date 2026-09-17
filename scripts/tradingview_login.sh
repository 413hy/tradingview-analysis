#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Stop worker to release persistent browser profile. Restart even after interrupted login.
docker compose --env-file deployment/.env -f deployment/compose.yml stop worker
trap 'docker compose --env-file deployment/.env -f deployment/compose.yml start worker' EXIT
export PYTHONPATH="$PWD/shared/src:$PWD/signal-system/src"
if [[ -n "${DISPLAY:-}" ]]; then
  .venv/bin/python scripts/run.py .venv/bin/python scripts/tradingview_login.py
else
  echo 'Use SSH X forwarding (ssh -Y) or install xvfb x11vnc novnc websockify for a localhost-only browser session.'
  for cmd in Xvfb x11vnc websockify; do command -v "$cmd" >/dev/null || exit 1; done
  Xvfb :98 -screen 0 1280x900x24 >/dev/null 2>&1 & xvfb_pid=$!
  for attempt in {1..30}; do
    [[ -S /tmp/.X11-unix/X98 ]] && break
    sleep 0.1
  done
  kill -0 "$xvfb_pid" || exit 1
  export DISPLAY=:98
  x11vnc -display :98 -localhost -nopw -rfbport 5908 -forever >/dev/null 2>&1 & vnc_pid=$!
  websockify --web=/usr/share/novnc 127.0.0.1:6088 localhost:5908 >/dev/null 2>&1 & web_pid=$!
  trap 'kill "$web_pid" "$vnc_pid" "$xvfb_pid" 2>/dev/null || true; docker compose --env-file deployment/.env -f deployment/compose.yml start worker' EXIT
  echo 'From your computer: ssh -L 6088:127.0.0.1:6088 root@YOUR_VPS'
  echo 'Then open http://127.0.0.1:6088/vnc.html ; the SSH tunnel is the authenticated entrance.'
  .venv/bin/python scripts/run.py .venv/bin/python scripts/tradingview_login.py
fi
