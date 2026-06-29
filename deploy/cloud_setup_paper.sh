#!/usr/bin/env bash
# 24/7 PAPER testing setup (NO wallet, NO private key — totally read-only).
set -euo pipefail
cd /opt/wxbot
echo "[1/3] system + python deps"
sudo apt-get update -y >/dev/null
sudo apt-get install -y python3-venv python3-pip git >/dev/null
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
mkdir -p data
echo "[2/3] install paper timer (ticks every 15 min) + tracking dashboard"
sudo cp deploy/wxbot-paper.service deploy/wxbot-paper.timer deploy/wxbot-dash.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wxbot-paper.timer
sudo systemctl enable --now wxbot-dash.service
echo "[3/3] kick off one paper scan now"
sudo systemctl start wxbot-paper.service || true
IP=$(curl -s ifconfig.me 2>/dev/null || echo YOUR_SERVER_IP)
echo
echo "DONE. Paper loop runs every 15 min, 24/7. NOTHING at risk."
echo "  TRACK ANYTIME:  http://$IP:8535   (Track Record tab)"
echo "  logs:           journalctl -u wxbot-paper -f"
