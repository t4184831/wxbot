# polymarket-weather (`wxbot`)

A toolkit to reverse-engineer and (carefully) replicate the profitable Polymarket
**daily-temperature** trader `0xB901…09fc4`.

> Status: research / paper-trading. Live execution exists but is hard-disabled
> behind env flags. **Nothing here trades real money on its own.**

## The trader we're copying

`0xB9012e0D9b60d3920286309328b935CDfA609fc4` (via synthesis.trade):

| | |
|---|---|
| Lifetime PnL | **+$32,837** since 2026-03-28 |
| Equity curve | 77 up-days vs 7 down-days (92% green) |
| Max drawdown | **−$1,171** (3.6% of profit) |
| Style | buy the meteorologically-favored bucket cheap, harvest convergence to $1 |
| Entry fingerprint | YES buys median **0.92**, NO buys median **0.99** |
| City rotation | Singapore-heavy, then Milan/Munich/Ankara/Istanbul/Amsterdam |

These markets ("Highest temperature in CITY on DATE?") are multi-outcome bucket
events that resolve off a **specific Wunderground airport station** (e.g. Austin =
`KAUS`) to whole-degree precision. Fahrenheit cities use 2°-wide buckets; Celsius
cities use 1°-wide buckets.

His edge is *informational*, not statistical: by mid/late afternoon local time the
day's high is largely observable on the resolution station, while the market still
prices residual uncertainty. He concentrates on low-variance, thinly-contested
cities (Singapore's high is ~31-32°C almost daily) and buys only when price lags
reality. The risk is negative skew (buy at 0.92, win ~8c / lose ~92c) → you need a
very high hit-rate, which demands accurate, *station-matched* data.

## The hard part (what the backtest taught us)

Naive replication **loses money**. Two reasons, both found empirically:

1. **Bucket parsing** — `"92-93°F"` must be read as the range 92→93, not `[92, -93]`.
   (Fixed; it silently corrupted every Fahrenheit market.)
2. **Data-source mismatch** — Open-Meteo's grid reanalysis differs from the
   Wunderground airport sensor by a **consistent per-station offset** (Milan +2°C,
   Munich −2°C). Raw, the model only lands the right bucket ~25% of the time.

The good news: the offset is *systematic*, so it can be calibrated away. After
per-station bias correction the residual error is small (~0.2–0.8°C). See the
calibration verdict below for whether grid data alone clears the bar, or whether
we must ingest the actual resolution-station observations (METAR).

## Architecture

```
wxbot/
  config.py            endpoints, costs, ICAO->lat/lon/tz station registry
  parse.py             Gamma event -> station + unit + numeric buckets
  clients/
    polymarket.py      Gamma + CLOB + Data API + PnL (read-only) + retry session
    weather.py         Open-Meteo obs / forecast / ensemble / archive (disk-cached)
  model.py             ensemble + intraday-floor -> calibrated bucket probabilities
  calibrate.py         per-station bias, leave-one-out honest accuracy
  recon.py             reverse-engineer any wallet
  backtest.py          score model vs RESOLVED outcomes, no lookahead, + PnL sim
  scanner.py           live edge: model prob vs order-book ask
  execution.py         PaperBroker (default) | LiveBroker (env-gated, limit-only)
scripts/               run_recon / run_calibrate / run_backtest / run_scan
```

## Usage

```bash
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python scripts/run_recon.py            # analyze the target trader
./.venv/bin/python scripts/run_calibrate.py 120    # build dataset + accuracy ceiling
./.venv/bin/python scripts/run_backtest.py 60 16,18 # honest backtest + PnL
./.venv/bin/python scripts/run_scan.py             # live edge on open markets
```

Live trading (only after paper validation clears the bar):
`WXBOT_LIVE=1 POLY_PK=… POLY_FUNDER=… ` + `pip install py-clob-client`.

## Calibration verdict — the decisive finding

Backtested on 88 of the trader's own resolved markets:

| Data source | Lands the resolved bucket | Notes |
|---|---|---|
| Open-Meteo grid, raw | ~22% | systematic per-station bias |
| Open-Meteo grid, bias-corrected (leave-one-out) | **~48%** | ceiling; obs-so-far at 3-6pm barely helps |
| **Station METAR/ASOS** (actual resolution source) | **~96%** | no bias correction needed |

**Conclusion: gridded weather cannot replicate this trader; the airport station's
own observations can.** The limiter isn't forecasting — it's that ERA5/grid is a
*different measurement* from the tarmac sensor the market resolves on. Reading the
resolution station's METAR late in the local afternoon (when the high is in) lands
the right bucket ~96% of the time. That is the trader's edge, and it's replicable.

Caveats before this prints money:
- **Predicting the bucket ≠ profit.** By the time METAR makes a bucket near-certain,
  the market has often already converged to 0.95-0.99 — you collect only the last
  cents (exactly his +1-3c scalps). Profit needs to beat the crowd's convergence,
  size up, harvest NO on dead buckets, and catch the occasional larger gap.
- **Remaining-day risk:** buy late enough that the diurnal peak has passed, or the
  high can still climb into a higher bucket. Tune the decision hour per city/season.
- **Execution:** thin books, fills, latency vs other bots, maker rewards.

Data sources: historical METAR = Iowa State IEM ASOS archive (free, by ICAO);
real-time METAR = aviationweather.gov (free, global). Both in `clients/metar.py`.

### Edge-vs-time (the crux), METAR floor, 79 of his resolved events

| Decision hour (local) | Favorite accuracy | Tradeable events | Trade hit-rate | PnL ($/set) |
|---|---|---|---|---|
| 13:00 (high not in yet) | 32.5% | 61 | 19.7% | **−2.26** |
| 15:00 (sweet spot)      | 73.8% | 33 | 51.5% | **+5.57** |
| 17:00 (high locked in)  | 97.5% | 10 | 80.0% | **+4.92** |

The edge is real but lives in a **narrow window**: early, you don't yet know the
high (you lose); late, you know it but the book has already converged to 0.97-0.99
so there's nothing left to buy (only ~10 of 79 events still tradeable). The money
is mid/late afternoon, *before* convergence. Magnitude is thin — a few dollars per
~30 markets/day — matching the real trader's grind ($32k on $millions of volume).

Honesty notes: PnL assumes fills at a price-history mid (real fills cross the spread,
so this is optimistic); n=79 is small; it excludes his NO-on-dead-buckets income and
maker rebates; and the 15:00 sweet spot risks overfitting to a 2-week sample (the
hour the high is "in" varies by city/season). An earlier buggy run reported +$14.68
by pricing US-city trades at their morning quote with afternoon knowledge — corrected
here.

### Live loop (paper) — what shaking it out taught us
The METAR floor is wired into `scanner.py` (`price_event_live`), with PaperBroker
routing and risk caps. Running it live surfaced two real lessons:

1. **Phantom longshots.** First pass "found" 0.001-priced tail buckets at p=1.0
   (+0.99 "edge"). That's not edge — when the whole book disagrees with you by 0.99,
   YOU are wrong (stale market / wrong station). Guardrails added: two-sided book
   required, only buy `0.20 ≤ ask ≤ 0.97`, reject edges > 0.45. Phantoms gone.
2. **The convergence wall is a liquidity wall.** By evening the winning bucket has
   *no sell-side* (everyone wants the near-certain winner, nobody offers) — you
   can't take the favorite at the close. The real game is the **mid-afternoon
   window**, acting as a **maker** (posting bids) before convergence, and selling
   into the run-up — exactly the target trader's pattern.

Also noted: with the high locked in, the model is **under-confident** (~0.65 vs the
~0.97 argmax accuracy the backtest showed) because the 0.5°C band is too wide late
in the day; tighten sigma as the local day closes.

## How he ACTUALLY trades (from his order tape)

His live orders revealed the real mechanism — and it's the opposite tail from a
naive "buy the cheap favorite" read:

- He buys **NO on near-dead buckets** (a specific degree the day's high has already
  passed, so it cannot be the high) at **0.97–0.99**, then sells at **0.999** — a
  ~1–3¢ convergence scalp, capital recycled fast. Examples from his tape: Singapore
  31°C NO 0.979→0.999 ($370), Ankara 25°C NO 0.973→0.999 ($316).
- This sidesteps the convergence/liquidity wall: the *winning* bucket's YES has no
  sell-side, but the *losing* buckets' NO is liquid and rich.

Implication: our first scanner hunted the wrong trade (cheap YES favorites, capped at
0.97) and so found ~nothing. The scanner now has a **NO-scalp mode** (`side="NO"`):
for buckets with `bucket.hi ≤ observed_high − margin` (factually dead) it buys NO when
the book agrees it's near-dead (0.90–0.995), harvesting the cents to 1.00. Verified
live (Shanghai 21°C NO @ 0.990, high 24°C).

**The honest catch:** the fat 2¢ exists only briefly, right after the high crosses a
bucket; by the time a periodic scan looks, NO is already ~0.999 (we measured 0–0.9¢
residual). So the real edge is a **maker game** — rest NO bids ~0.98 on soon-to-be-
dead buckets and get filled as the high crosses. Taker-scanning captures only the
residual; maker fills can't be paper-tested → that's Gate B.

Risk is **data reliability, not market risk**: a wrong/stale station read could call a
live bucket "dead" (−98¢ tail). Hence the whole-degree `no_scalp_margin` below the
observed high, keyed strictly to the resolution ICAO. His rare red days are almost
certainly boundary/data misses.

## Dashboard

```bash
./.venv/bin/streamlit run dashboard.py        # -> http://localhost:8535
lsof -ti tcp:8535 | xargs kill -9             # stop it
```

Five tabs: **Opportunities** (live scan; each candidate as a card with edge, depth,
obs-high/margin and the three safety checks ✅/❌ + a Wunderground verify link),
**Backtester** (resolved-market backtest; "sweep hours" reproduces the edge-vs-time
curve + per-city PnL), **Track Record** (Gate A paper positions, settled to real
resolution, cumulative-PnL chart), **Target Trader** (his stats / rotation / orders),
**How it works**. Engine = `wxbot/opportunities.py` (classifies SCALP / STALE_DISCOUNT
/ FAVORITE and runs the safety checks). Theme/port in `.streamlit/config.toml`.

## Gate A — the paper proof (RUNNING)

A LaunchAgent runs `scripts/run_paper.py` every 30 min: it scans live afternoon
windows, records each tradeable signal as a paper position (`data/paper_positions.json`),
and settles matured positions against the REAL resolution. This measures whether
capturable, plausibly-priced signals actually appear and whether realized PnL is
positive after marking to truth. Confidence is now calibrated (~0.98 on locked-in
cities) and the settle path is verified on a resolved event.

```bash
# check the running track record any time:
./.venv/bin/python -c "import sys;sys.path.insert(0,'.');from wxbot.paper import settle,report;settle();report()"
tail -f data/paper.log              # see each 30-min run
launchctl list | grep wxbot        # confirm the agent is loaded
launchctl unload ~/Library/LaunchAgents/com.wxbot.paper.plist   # stop it
```

Let it run ~1-2 weeks. Decision rule: only proceed to Gate B if settled paper PnL is
clearly positive AND tradeable signals appear with usable frequency/size.

### Gate B — small real money (the only test of maker fills)
Because the winning bucket usually has no sell-side by the time we're certain, the
real strategy is **maker** (post bids in the mid-afternoon window) — and maker fills
can't be simulated. After Gate A: implement maker posting in `execution.py`, fund a
small amount, and run via the env-gated `LiveBroker` (`WXBOT_LIVE=1` + `POLY_PK` +
`POLY_FUNDER`). YOU place/fund; the bot never moves money implicitly.
