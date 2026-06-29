"""WxEdge — Polymarket weather-market opportunity dashboard.

  ./.venv/bin/streamlit run dashboard.py      # -> http://localhost:8535

Tabs: Opportunities (live edge + safety checks) · Backtester · Track Record · Trader.
"""
import sys, json, os
from datetime import datetime, timezone, date
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wxbot.opportunities import find_opportunities
from wxbot import backtest as bt
from wxbot import recon, paper
from wxbot.clients import Polymarket
from wxbot.config import COSTS

st.set_page_config(page_title="WxEdge · Polymarket Weather", layout="wide", page_icon="🌡️")

st.markdown("""
<style>
  .block-container {padding-top: 1.2rem; max-width: 1500px;}
  h1,h2,h3 {letter-spacing:-0.01em;}
  [data-testid="stMetricValue"] {font-size: 1.5rem;}
  .pill {padding:2px 9px;border-radius:999px;font-size:0.72rem;font-weight:700;}
  .tradeable{background:#0f5132;color:#5ff0a0;}
  .watch{background:#5c4600;color:#ffd24d;}
  .blocked{background:#5c1a1a;color:#ff8a8a;}
  .muted{color:#7d8590;font-size:0.85rem;}
  .big{font-size:2.0rem;font-weight:800;line-height:1;}
  .sub{color:#7d8590;font-size:0.8rem;text-transform:uppercase;letter-spacing:.06em;}
  table {font-size:0.86rem;}
</style>
""", unsafe_allow_html=True)


# ---------------- cached data loaders ----------------
@st.cache_data(ttl=180, show_spinner=False)
def scan_opps(pages, only_afternoon, include_favorites):
    return [o.to_row() for o in find_opportunities(pages=pages,
            include_favorites=include_favorites, only_afternoon=only_afternoon)]

@st.cache_data(ttl=900, show_spinner=False)
def trader_stats():
    try:
        return recon.analyze(verbose=False)
    except Exception as e:
        return {"error": str(e)}

@st.cache_data(ttl=900, show_spinner=False)
def trader_orders(limit=40):
    try:
        pm = Polymarket()
        acts = pm.activity(recon.TARGET, limit=limit)
        rows = []
        for a in acts[:limit]:
            rows.append({"time": datetime.utcfromtimestamp(a.get("timestamp", 0)).strftime("%m-%d %H:%M"),
                         "side": a.get("side"), "market": (a.get("title") or "")[:60],
                         "outcome": a.get("outcome"), "price": a.get("price"),
                         "size": a.get("size"), "usdc": a.get("usdcSize")})
        return rows
    except Exception as e:
        return [{"error": str(e)}]

@st.cache_data(ttl=900, show_spinner=False)
def run_bt(limit, hours_tuple, source, universe, strategy, pages):
    s = bt.run(events_limit=limit, hours=list(hours_tuple), source=source,
               universe=universe, strategy=strategy, pages=pages, verbose=False)
    s.pop("trials", None)
    return s


def pill(v):
    cls = {"TRADEABLE": "tradeable", "WATCH": "watch", "BLOCKED": "blocked"}.get(v, "muted")
    return f'<span class="pill {cls}">{v}</span>'


# ---------------- header ----------------
ts_ = trader_stats()
c1, c2, c3, c4, c5 = st.columns([3, 1, 1, 1, 1])
with c1:
    st.markdown("## 🌡️ WxEdge")
    st.markdown('<span class="muted">Polymarket daily-temperature edge scanner · '
                'METAR-anchored · NO-scalp / stale-discount / favorite</span>', unsafe_allow_html=True)
if "error" not in ts_:
    c2.metric("Target PnL", f"${ts_.get('final_pnl', 0):,.0f}")
    c3.metric("Day win-rate", f"{(ts_.get('day_win_rate') or 0)*100:.0f}%")
    c4.metric("Max DD", f"${ts_.get('max_drawdown', 0):,.0f}")
    c5.metric("Trades", f"{ts_.get('total_trades', ts_.get('n_trades_sampled','-'))}")

tabs = st.tabs(["🎯 Opportunities", "📊 Backtester", "📈 Track Record", "👤 Target Trader", "ℹ️ How it works"])

# ============================================================ OPPORTUNITIES
with tabs[0]:
    with st.sidebar:
        st.header("Scan settings")
        pages = st.slider("Event pages to scan", 2, 8, 5)
        only_pm = st.checkbox("Afternoon windows only", True,
                              help="Only cities currently past midday on the resolution day "
                                   "— the only time the high is observable.")
        inc_fav = st.checkbox("Include YES-favorites", True)
        st.divider()
        kinds = st.multiselect("Opportunity type", ["SCALP", "STALE_DISCOUNT", "FAVORITE"],
                               ["SCALP", "STALE_DISCOUNT", "FAVORITE"])
        verdicts = st.multiselect("Verdict", ["TRADEABLE", "WATCH", "BLOCKED"],
                                  ["TRADEABLE", "WATCH"])
        min_edge = st.slider("Min edge ($/share)", 0.0, 0.5, 0.0, 0.005)
        if st.button("🔄 Rescan (clear cache)", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    with st.spinner("Scanning live markets (weather + METAR + order books)…"):
        rows = scan_opps(pages, only_pm, inc_fav)

    if not rows:
        st.info("No opportunities surfaced right now. Either no city is in its afternoon "
                "window, or favorites have already converged (no sell-side). This is the "
                "expected resting state — try again later or widen the scan.")
    else:
        df = pd.DataFrame(rows)
        df = df[df["kind"].isin(kinds) & df["verdict"].isin(verdicts) & (df["edge"] >= min_edge)]
        t = (df["verdict"] == "TRADEABLE").sum()
        w = (df["verdict"] == "WATCH").sum()
        b = (df["verdict"] == "BLOCKED").sum()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("✅ Tradeable", int(t))
        m2.metric("👀 Watch", int(w))
        m3.metric("⛔ Blocked", int(b))
        m4.metric("Best edge", f"{df['edge'].max()*100:.1f}¢" if len(df) else "—")

        for _, r in df.iterrows():
            star = "⭐" if r["preferred"] else ""
            with st.expander(
                f"{pill(r['verdict'])}  **{r['side']} {r['city']} {star} · {r['bucket']}**  "
                f"· {r['kind']} · edge **{r['edge']*100:+.1f}¢** · ask {r['ask']}", expanded=False):
                cc = st.columns(4)
                cc[0].markdown(f"**Obs high**: {r['obs_high']}{r['unit']}  \n"
                               f"**Margin**: {r['margin_deg'] if r['margin_deg'] is not None else '—'}°  \n"
                               f"**Local hour**: {r['local_hour']}")
                cc[1].markdown(f"**Bid / Ask**: {r['bid']} / {r['ask']}  \n"
                               f"**Room→1.00**: {r['room_cents']}¢  \n"
                               f"**Depth**: ${r['depth_usd']:,.0f}")
                cc[2].markdown(f"**Model P({r['side']})**: {r['model_prob']:.3f}  \n"
                               f"**Station**: {r['icao']}  \n"
                               f"**Day**: {r['day']}")
                ok = lambda x: "✅" if x else "❌"
                cc[3].markdown(f"**Station confirmed**: {ok(r['station_ok'])}  \n"
                               f"**Resolution day**: {ok(r['on_day'])}  \n"
                               f"**Dead-by-margin**: {ok(r['margin_ok'])}")
                if r["reasons"]:
                    st.markdown('<span class="muted">⚠️ ' + " · ".join(r["reasons"]) + "</span>",
                                unsafe_allow_html=True)
                if r["wunderground"]:
                    st.markdown(f"[Verify on Wunderground →]({r['wunderground']})")

        with st.expander("Full table"):
            show = df.drop(columns=["reasons", "wunderground", "token_id", "market_id", "event_id"])
            st.dataframe(show, use_container_width=True, hide_index=True)

# ============================================================ BACKTESTER
with tabs[1]:
    st.markdown("#### Backtest resolved markets (no lookahead)")
    bc1, bc2, bc3 = st.columns(3)
    strategy = bc1.radio("Strategy", ["noscalp", "favorite"], horizontal=True,
                         format_func=lambda s: {"noscalp": "NO-scalp (his game)",
                                                "favorite": "YES favorite"}[s])
    universe = bc2.radio("Universe", ["trader", "discover"], horizontal=True,
                         format_func=lambda u: {"trader": "His markets",
                                                "discover": "ALL resolved temp markets"}[u])
    source = bc3.radio("Data source", ["metar", "grid"], horizontal=True,
                       help="metar = real station obs (resolution source). grid = Open-Meteo (~48%).")
    bd1, bd2, bd3 = st.columns(3)
    limit = bd1.slider("Resolved events", 20, 200, 60, 10)
    pages = bd2.slider("Discover pages (universe=ALL)", 2, 12, 5,
                       help="More pages → larger all-markets sample (slower).")
    sweep = bd3.checkbox("Sweep hours 13/15/17", True)
    hours = [13, 15, 17] if sweep else [bd3.slider("Decision hour (local)", 12, 19, 17)]

    if st.button("▶ Run backtest", type="primary"):
        with st.spinner(f"Backtesting {strategy} on {universe} ({source}) — "
                        f"{limit} events × {len(hours)} hour(s)…"):
            if strategy == "favorite":
                recs = []
                for h in hours:
                    s = run_bt(limit, (h,), source, universe, "favorite", pages)
                    if s.get("n"):
                        recs.append({"hour": h, **{k: s[k] for k in
                                     ("n", "hit_rate", "brier", "n_traded", "total_pnl")},
                                     "_by_city": s["by_city"]})
                st.session_state["bt"] = {"strategy": "favorite", "recs": recs}
            else:
                s = run_bt(limit, tuple(hours), source, universe, "noscalp", pages)
                st.session_state["bt"] = {"strategy": "noscalp", "summary": s}

    res = st.session_state.get("bt")
    if not res:
        st.info("Pick a strategy + universe and hit **Run backtest**. "
                "**NO-scalp** is the trader's real game — its win-rate is the safety metric "
                "(should be ~100%; misses = station/resolution mismatch). "
                "**Sweep** (favorite) reproduces the edge-vs-time curve.")
    elif res["strategy"] == "noscalp":
        s = res["summary"]
        if not s.get("n"):
            st.warning(s.get("note", "no trials"))
        else:
            k = st.columns(4)
            k[0].metric("✅ Win-rate (resolved NO)", f"{s['win_rate']*100:.1f}%")
            k[1].metric("Avg entry", f"{s['avg_entry']:.3f}")
            k[2].metric("Dead-bucket trials", s["n"])
            k[3].metric("Total PnL ($)", f"{s['total_pnl']:+.2f}")
            st.caption("**Win-rate = % of buckets we called 'dead' that actually resolved NO.** "
                       "Anything under 100% is the station/resolution-mismatch tail (a −98¢ event). "
                       "Note: plain scalps net ≈0 after friction — the edge is the stale-discount tranche.")
            split = pd.DataFrame([
                {"tranche": "SCALP (NO ≥ 0.90)", "n": s["n_scalp"],
                 "win %": round((s["scalp_winrate"] or 0)*100, 1), "PnL $": round(s["scalp_pnl"], 2)},
                {"tranche": "STALE_DISCOUNT (NO < 0.90)", "n": s["n_stale"],
                 "win %": round((s["stale_winrate"] or 0)*100, 1), "PnL $": round(s["stale_pnl"], 2)}])
            mdf = pd.DataFrame([{"margin °": m, "n": d["n"], "win %": round(d["win_rate"]*100, 1),
                                 "avg PnL $": round(d["avg_pnl"], 4)} for m, d in s["by_margin"].items()])
            cc1, cc2 = st.columns(2)
            with cc1:
                st.markdown("**By tranche**"); st.dataframe(split, use_container_width=True, hide_index=True)
                st.markdown("**By safety margin (degrees below high)**")
                st.dataframe(mdf, use_container_width=True, hide_index=True)
            with cc2:
                fig = px.bar(mdf, x="margin °", y="win %", template="plotly_dark", height=300,
                             title="Win-rate by margin (all should be ~100%)", range_y=[90, 101])
                fig.update_traces(marker_color="#22c55e"); fig.update_layout(margin=dict(t=40, b=10))
                st.plotly_chart(fig, use_container_width=True)
    else:  # favorite
        recs = res["recs"]
        if not recs:
            st.warning("No scored trials (data/archive gaps for this universe). "
                       "For ALL-markets, raise Discover pages.")
        else:
            last = recs[-1]
            k = st.columns(4)
            k[0].metric("Favorite accuracy", f"{last['hit_rate']*100:.1f}%")
            k[1].metric("Brier", f"{last['brier']:.3f}")
            k[2].metric("Trades", last["n_traded"])
            k[3].metric("PnL ($/set)", f"{last['total_pnl']:+.2f}")
            if len(recs) > 1:
                edf = pd.DataFrame([{"hour": f"{r['hour']}:00", "accuracy %": r["hit_rate"]*100,
                                     "PnL $": r["total_pnl"]} for r in recs])
                fig = go.Figure()
                fig.add_bar(x=edf["hour"], y=edf["accuracy %"], name="accuracy %", marker_color="#3b82f6")
                fig.add_scatter(x=edf["hour"], y=edf["PnL $"], name="PnL $", yaxis="y2",
                                mode="lines+markers", marker_color="#22c55e")
                fig.update_layout(title="Edge vs decision hour (later = accurate but converged)",
                                  yaxis=dict(title="accuracy %"),
                                  yaxis2=dict(title="PnL $", overlaying="y", side="right"),
                                  template="plotly_dark", height=360, margin=dict(t=40, b=10))
                st.plotly_chart(fig, use_container_width=True)
            cdf = pd.DataFrame([{"city": c, "n": d["n"], "accuracy %": round(d["hit_rate"]*100, 1),
                                 "PnL $": round(d["pnl"], 2)} for c, d in last["_by_city"].items()])
            cc1, cc2 = st.columns(2)
            with cc1:
                st.dataframe(cdf, use_container_width=True, hide_index=True)
            with cc2:
                fig2 = px.bar(cdf.sort_values("PnL $"), x="PnL $", y="city", orientation="h",
                              color="PnL $", color_continuous_scale=["#ef4444", "#6b7280", "#22c55e"],
                              template="plotly_dark", height=380)
                fig2.update_layout(margin=dict(t=10, b=10), coloraxis_showscale=False)
                st.plotly_chart(fig2, use_container_width=True)

# ============================================================ TRACK RECORD
with tabs[2]:
    st.markdown("#### Paper track record (Gate A) — marked to real resolution")
    if st.button("↻ Settle & refresh"):
        try:
            paper.settle(verbose=False)
        except Exception as e:
            st.warning(f"settle: {e}")
    p = Path("data/paper_positions.json")
    pos = json.loads(p.read_text()) if p.exists() else []
    if not pos:
        st.info("No paper positions yet. The scheduled scanner (every 10 min) records "
                "tradeable signals here and settles them when each market resolves.")
    else:
        rep = paper.report()  # prints; also returns dict
        closed = [r for r in pos if r["status"] in ("WON", "LOST")]
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("Open", sum(r["status"] == "OPEN" for r in pos))
        r2.metric("Settled", len(closed))
        if closed:
            wr = sum(r["status"] == "WON" for r in closed) / len(closed)
            pnl = sum(r["pnl"] or 0 for r in closed)
            r3.metric("Win rate", f"{wr*100:.0f}%")
            r4.metric("Realized PnL", f"${pnl:+.2f}")
            cum = pd.DataFrame(sorted(closed, key=lambda r: r["ts"]))
            cum["trade #"] = range(1, len(cum) + 1)
            cum["cum_pnl"] = cum["pnl"].cumsum()
            fig = px.area(cum, x="trade #", y="cum_pnl", template="plotly_dark",
                          height=280, title="Cumulative paper PnL")
            st.plotly_chart(fig, use_container_width=True)
        st.dataframe(pd.DataFrame(pos)[["status", "side", "city", "day", "bucket",
                     "entry", "stake", "pnl"]], use_container_width=True, hide_index=True)

# ============================================================ TARGET TRADER
with tabs[3]:
    st.markdown(f"#### Target: `{recon.TARGET}`")
    if "error" in ts_:
        st.error(ts_["error"])
    else:
        g = st.columns(4)
        g[0].metric("Lifetime PnL", f"${ts_.get('final_pnl',0):,.0f}")
        g[1].metric("Up / Down days", f"{ts_.get('up_days','-')} / {ts_.get('down_days','-')}")
        g[2].metric("YES-buy median", f"{ts_.get('yes_buy_median','-')}")
        g[3].metric("NO-buy median", f"{ts_.get('no_buy_median','-')}")
        flow = ts_.get("city_flow", {})
        if flow:
            fdf = pd.DataFrame([{"city": c, "trades": v["n"], "net $": round(v["net"], 0)}
                               for c, v in flow.items()]).sort_values("trades", ascending=False)
            st.markdown("**City rotation (by trade count)**")
            st.dataframe(fdf, use_container_width=True, hide_index=True)
    st.markdown("**Recent orders**")
    st.dataframe(pd.DataFrame(trader_orders()), use_container_width=True, hide_index=True)

# ============================================================ HOW IT WORKS
with tabs[4]:
    st.markdown(f"""
#### The strategy in one screen

**What resolves these markets:** the day's highest temperature at one specific airport
station (the ICAO in the market's Wunderground URL), to the whole degree.

**The edge:** read that exact station's live observation (METAR). Grid weather lands the
resolved bucket ~48%; the station's own obs ~96%.

**The trades the scanner surfaces**
- `SCALP` — buy **NO** on a bucket the observed high has already passed (it cannot be the
  high → NO worth $1.00), at 0.97–0.99, harvest the last cents. The trader's main game.
- `STALE_DISCOUNT` — same dead bucket but an illiquid book leaves NO cheap (<0.90). Huge
  edge *if* truly dead — treated as **WATCH** until verified.
- `FAVORITE` — buy **YES** on the model's favorite when the book underprices it (rare;
  the winner usually has no sell-side late in the day).

**The three safety checks on every row** (a too-good price is usually our error, not free money)
1. **Station confirmed** — the ICAO came from the market's own resolution URL (not a guess).
2. **Resolution day** — it's actually that market's local day (a future-day market must
   produce *no* signal).
3. **Dead-by-margin** — the bucket is below the observed high by ≥ {COSTS.no_scalp_margin}°,
   so a 1° data discrepancy can't flip it.

**Status:** Gate A (paper, running every 10 min) is proving whether capturable signals
appear and settle positive. Gate B (small real money) tests **maker fills** — resting NO
bids at ~0.98 on soon-dead buckets — which paper cannot simulate.
""")

st.caption(f"scanned {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · data: Polymarket "
           "Gamma/CLOB · weather: Open-Meteo + aviationweather/IEM METAR")
