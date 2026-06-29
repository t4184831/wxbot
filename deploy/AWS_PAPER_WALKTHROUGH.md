# Launch 24/7 PAPER testing on AWS — exact steps

This runs the weather bot in **paper mode**: it scans live markets every 15 min,
logs the bets it *would* place, and scores them against real outcomes — **no
wallet, no private key, nothing at risk.** You track it from any browser.
Total ~15 min, ~$5/month (free-tier eligible the first year).

---

## PART 1 — Put the code online (you: ~6 clicks · me: the push)
1. Go to **github.com** → sign up if needed (free).
2. **+** (top-right) → **New repository** → name `wxbot` → **Public** → **don't**
   add a README → **Create repository**.
3. Make an upload token: avatar → **Settings** → **Developer settings** →
   **Personal access tokens → Tokens (classic)** → **Generate new token** →
   tick **`repo`** → **Generate** → copy it (`ghp_…`).
4. **Paste me: your GitHub username + the token.** I'll push the code and
   confirm. (Token = temporary password; delete it after. Secrets are
   git-ignored, so nothing sensitive goes up.)

## PART 2 — Make the AWS server (AWS Lightsail = simplest)
1. **lightsail.aws.amazon.com** → sign in / create AWS account.
2. **Create instance** → **Linux/Unix** → **OS Only → Ubuntu 24.04**.
3. Plan: cheapest **$5/mo** (or $3.50 — fine). → **Create instance**. Wait ~1 min.
4. Open the dashboard port: click the instance → **Networking** tab →
   **Add rule** → **Custom**, **TCP**, port **8535** → **Create**. (Lets you
   view the tracker from your browser.)

## PART 3 — Turn it on (paste 3 lines)
1. On the instance, click the **terminal icon** ("Connect using SSH") — a black
   terminal opens in your browser.
2. Paste these one at a time (replace `YOURNAME`):
   ```bash
   sudo git clone https://github.com/YOURNAME/wxbot.git /opt/wxbot
   ```
   ```bash
   cd /opt/wxbot && sudo bash deploy/cloud_setup_paper.sh
   ```
3. When it prints **"DONE … TRACK ANYTIME: http://<IP>:8535"**, it's live. 🎉
   The paper loop now runs every 15 min, 24/7, even with your Mac off.

## PART 4 — Track it anytime
- **Open `http://<YOUR-INSTANCE-IP>:8535`** in any browser (the IP is on the
  Lightsail instance page, and in the DONE message). Go to the **Track Record**
  tab — paper P&L, win rate, open/settled positions, updating every 15 min.
- Bookmark it. Check from your phone, anywhere.

## Watch / stop (optional, in the SSH terminal)
```bash
journalctl -u wxbot-paper -f                 # live paper-scan logs (Ctrl-C to exit)
sudo systemctl status wxbot-paper.timer      # is the 15-min loop active?
sudo systemctl disable --now wxbot-paper.timer wxbot-dash.service   # stop everything
```

---

## What we're looking for (the validation, before any real money)
Let it run **1–2 weeks**, then check the Track Record tab for:
- **Do the bets actually appear** (is the edge there live, not just in backtest)?
- **Win rate** vs the backtest's 100% — the real station-mismatch tail will show.
- **Are the prices real** (could you have actually filled)?

If it's green and the fills look real, *then* we talk real money — **burner
wallet, ~5–10% sizing (not 20%), small cap, kill switch ready.** Not before.
