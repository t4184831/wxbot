# 24/7 cloud deployment (laptop can be off)

Honest heads-up: this is the hardest part for a non-technical user — a real server, the
command line, and your **trading key living on a remote machine**. Do it carefully.

## ⚠️ Before you start — the one rule that bounds your risk
**Use a brand-new "burner" Polymarket wallet, funded with ONLY the test money** (e.g.
$100–150). The private key goes on the server; if that server is ever compromised, only
what's in that wallet can be lost. **Never put your main wallet's key on the server.**

## Step 0 — attended check on your laptop FIRST (do not skip)
Run a couple of live cycles while watching, per `DEPLOY.md` §2–3. Confirm orders place,
cancel, and (ideally) fill, with `--max-orders 2 --cap 100`. Only proceed once that's clean.

## Step 1 — get the code onto GitHub (I can do this for you)
The server needs a copy of this project. Easiest: a **private GitHub repo**. Ask me to
"push the project to GitHub" — I'll `git init`, exclude secrets via `.gitignore`, and push.
You'll get a URL like `https://github.com/youruser/polymarket-weather`.

## Step 2 — create the server (DigitalOcean, ~$6/month)
1. Sign up at digitalocean.com → **Create → Droplet**.
2. Image: **Ubuntu 24.04 (LTS)**. Plan: **Basic, $6/mo** (1 GB) is plenty.
3. Auth: choose **Password** (simplest) and set a strong root password.
4. Create. When it's ready, click **Console** (top-right) — a terminal opens in your browser.
   No SSH setup needed.

## Step 3 — install (paste into the browser Console)
```bash
cd /opt && git clone https://github.com/YOURUSER/polymarket-weather wxbot && cd wxbot
bash deploy/cloud_setup.sh
```
(If the repo is private, GitHub will ask for your username + a Personal Access Token —
create one at github.com → Settings → Developer settings → Tokens, scope `repo`.)

## Step 4 — add your keys (browser Console)
```bash
cat > /opt/wxbot/wxbot.env <<'EOF'
WXBOT_LIVE=1
POLY_PK=0xYOUR_BURNER_PRIVATE_KEY
POLY_FUNDER=0xYOUR_PROXY_ADDRESS
EOF
chmod 600 /opt/wxbot/wxbot.env
```

## Step 5 — paper sanity check on the server (no money)
```bash
cd /opt/wxbot && ./.venv/bin/python scripts/run_maker.py --max-orders 4 --cap 200
```
Should print planned NO bids in verified cities. If yes, go live.

## Step 6 — start the 24/7 loop
```bash
systemctl enable --now wxbot-maker      # starts now + on every reboot
journalctl -u wxbot-maker -f            # watch it live (Ctrl-C just stops watching)
```

## Step 7 — monitor & stop
- **Watch:** `journalctl -u wxbot-maker -f`, and your positions in the Polymarket app.
- **Kill switch (cancel all resting orders):**
  `cd /opt/wxbot && ./.venv/bin/python scripts/run_maker.py --live --cancel-all`
- **Stop the bot:** `systemctl stop wxbot-maker`
- **Stop + cancel everything:** run both of the above.

## Costs & honest expectations
- Server: ~$6/mo. The edge is **thin and unproven** (no reward subsidy; maker fills
  untested) — 24/7 runs it more, it does not make it bigger.
- Caps start tiny (`--max-orders 2 --cap 100`). Raise them only after a week of clearly
  positive, real fills. If fills don't happen or PnL isn't positive, **stop** — the server
  fee alone would make a no-edge bot a slow loss.
- Check it at least daily for the first week. "Unattended" still means "watched regularly."
