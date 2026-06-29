# Live test runbook (Gate B) — small account

**Goal of this test:** find out whether maker NO-bids actually *fill* and whether the
thin convergence-spread + rare stale-discounts net positive **for us**, on a small
stake. It is a *learning* test, not a profit engine. Read the honest expectations.

---

## ⚠️ Honest expectations (read first)
- **No reward subsidy.** These daily-temperature markets show `rewards.rates = null` — the
  Polymarket liquidity program is **not** funding them. So the only edge is (1) maker
  spread capture (buy NO ~0.97 → ~1.00) and (2) rare stale-discounts. Both thin.
- **Maker fills are unproven for us** and cannot be simulated — that's the whole reason
  for this test. Your bids may simply not get hit (the winning bucket's NO often has no
  sellers).
- **Safety is solid, profit is not.** Backtest: 100% dead-call win-rate across 14 verified
  cities (no blow-up tail) — but the plain scalp is ≈break-even after costs. Expect to
  *learn*, possibly lose a few dollars, not to print money.
- Start with the smallest size that qualifies for an order. Scale only if it works.

## What the bot does / won't do
- Trades **NO** on buckets the observed high has already passed, **only in the 14 verified
  cities** (`data/verified_cities.json`), only on the resolution day, only dead-by-margin.
- Caps: `--stake` per market, `--cap` total exposure. `max_buy_price` guard.
- **Never** moves money on its own. Live orders require `WXBOT_LIVE=1` **and** both keys.
- **Auto-reconcile:** `--loop` re-prices/cancels resting orders as the high moves and
  removes them when a bucket resolves or leaves the frontier. Skips all cancels if the
  scan returns no events (transient-failure guard). `--cancel-all` is the kill switch.
- Maker orders are **post-only** (never cross into a taker fill).

---

## 1. Prerequisites (you do these)
1. Funded Polymarket account with **USDC on Polygon**. Use a **dedicated small wallet**,
   not your main one.
2. Get your **proxy (funder) wallet address** — your Polymarket deposit address.
3. Export your **EOA private key** (the signer). Treat it like a password.

## 2. Dry-run first (no money)
```bash
cd /Users/yc/Claude/Projects/polymarket-weather
./.venv/bin/python scripts/run_maker.py --max-orders 4 --cap 200      # paper reconcile
```
Confirm the planned orders look sane (verified cities, NO on dead buckets, prices ≤0.985).

## 3. Go live (small)
```bash
export WXBOT_LIVE=1
export POLY_PK=0xYOUR_PRIVATE_KEY        # dedicated wallet; never commit/log this
export POLY_FUNDER=0xYOUR_PROXY_ADDRESS
# one reconcile cycle, tiny:
./.venv/bin/python scripts/run_maker.py --live --max-orders 2 --cap 100
# or auto-reconcile every 5 min during a verified city's local afternoon:
./.venv/bin/python scripts/run_maker.py --live --loop 300 --max-orders 2 --cap 100
```
- `--max-orders 2 --cap 100` keeps the first test tiny (~2 orders, ≤$100 at risk).
- Run during a verified city's local **afternoon** (when its high is forming/in).

## 4. Monitor & stop
- Watch fills in the Polymarket UI / your positions.
- **Kill switch:** `./.venv/bin/python scripts/run_maker.py --live --cancel-all`
- **Stop the loop:** Ctrl-C (resting orders stay; run `--cancel-all` to clear), then `unset WXBOT_LIVE`.
- Record: did bids fill? at what price? did they settle to $1.00? net after the day.

## 5. Decision
Scale up only if: bids actually fill **and** net PnL over a week of small tests is
clearly positive. Otherwise the honest conclusion is "real but not capturable by us" —
and we stop, exactly as the discipline demands.

---
Built on Python 3.12 venv (`.venv`). Live order path = `wxbot/execution.py:LiveBroker`
(limit orders via py-clob-client). Strategy logic = `wxbot/maker.py`.
