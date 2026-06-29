#!/usr/bin/env bash
# One-shot server setup. Run as root on a FRESH Ubuntu 24.04 server, AFTER the code
# is present at /opt/wxbot (see CLOUD_DEPLOY.md). Idempotent — safe to re-run.
set -euo pipefail
APP=/opt/wxbot

echo "==> installing python3.12 + venv"
apt-get update -y
apt-get install -y python3.12-venv python3-pip

echo "==> building venv at $APP/.venv"
cd "$APP"
python3.12 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip -q
# server runs the maker loop only — no streamlit/plotly needed (lighter, faster)
./.venv/bin/python -m pip install -q requests numpy py-clob-client

echo "==> verifying imports"
./.venv/bin/python -c "import wxbot.maker, py_clob_client; print('imports OK', __import__('sys').version.split()[0])"

echo "==> installing systemd service"
install -m 644 deploy/wxbot-maker.service /etc/systemd/system/wxbot-maker.service
systemctl daemon-reload

echo
echo "Setup complete. Next:"
echo "  1) create $APP/wxbot.env  (WXBOT_LIVE=1 / POLY_PK / POLY_FUNDER), then: chmod 600 $APP/wxbot.env"
echo "  2) PAPER reconcile sanity check (no money):"
echo "       ./.venv/bin/python scripts/run_maker.py --max-orders 4 --cap 200"
echo "  3) start the live loop:   systemctl enable --now wxbot-maker"
echo "     watch it:              journalctl -u wxbot-maker -f"
echo "     stop everything:       systemctl stop wxbot-maker && ./.venv/bin/python scripts/run_maker.py --live --cancel-all"
