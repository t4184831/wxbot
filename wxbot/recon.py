"""Reverse-engineer a target trader from public Polymarket data.

Pulls cumulative PnL, current positions, and recent trades; reports the equity
curve quality, city rotation, entry-price fingerprint, and per-city net flow —
i.e. everything that tells us what playbook to copy.
"""
from __future__ import annotations
import re
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Dict

from .clients import Polymarket

TARGET = "0xB9012e0D9b60d3920286309328b935CDfA609fc4"


def _city(title: str) -> str:
    m = re.search(r"temperature in ([A-Za-z .'-]+?) (?:be|between|on)", title)
    return m.group(1).strip() if m else "?"


def analyze(user: str = TARGET, verbose: bool = True) -> Dict:
    pm = Polymarket()
    pnl = pm.user_pnl(user)
    acts = pm.activity(user, limit=1000)

    pts = [(p["t"], p["p"]) for p in pnl]
    peak, maxdd, up, dn, prev = -1e18, 0.0, 0, 0, None
    for _, p in pts:
        peak = max(peak, p)
        maxdd = min(maxdd, p - peak)
        if prev is not None:
            up += p > prev
            dn += p < prev
        prev = p
    final = pts[-1][1] if pts else 0.0

    buys = [a for a in acts if a.get("side") == "BUY"]
    sells = [a for a in acts if a.get("side") == "SELL"]
    bp = [a["price"] for a in buys]
    yes = [a["price"] for a in buys if a.get("outcome") == "Yes"]
    no = [a["price"] for a in buys if a.get("outcome") == "No"]

    flow = defaultdict(lambda: [0.0, 0.0, 0])  # buy$, sell$, n
    for a in acts:
        c = _city(a.get("title", ""))
        u = a.get("usdcSize", 0.0)
        flow[c][0 if a["side"] == "BUY" else 1] += u
        flow[c][2] += 1

    out = {
        "final_pnl": final,
        "max_drawdown": maxdd,
        "up_days": up, "down_days": dn,
        "day_win_rate": up / (up + dn) if (up + dn) else None,
        "n_trades_sampled": len(acts),
        "span": (datetime.utcfromtimestamp(min(a["timestamp"] for a in acts)).date().isoformat(),
                 datetime.utcfromtimestamp(max(a["timestamp"] for a in acts)).date().isoformat()) if acts else None,
        "buy_price_median": statistics.median(bp) if bp else None,
        "yes_buy_median": statistics.median(yes) if yes else None,
        "no_buy_median": statistics.median(no) if no else None,
        "city_flow": {c: {"buy": v[0], "sell": v[1], "net": v[1] - v[0], "n": v[2]}
                       for c, v in sorted(flow.items(), key=lambda x: -(x[1][1] - x[1][0]))},
    }
    if verbose:
        _print(user, out)
    return out


def _print(user: str, o: Dict) -> None:
    print("=" * 60)
    print(f"TRADER RECON  {user}")
    print("=" * 60)
    print(f"lifetime PnL        : ${o['final_pnl']:,.0f}")
    print(f"max drawdown        : ${o['max_drawdown']:,.0f}")
    print(f"up / down days      : {o['up_days']} / {o['down_days']}  "
          f"({(o['day_win_rate'] or 0)*100:.0f}% green)")
    print(f"recent trades window: {o['span']}  (n={o['n_trades_sampled']})")
    print(f"BUY price median    : {o['buy_price_median']}  "
          f"(YES {o['yes_buy_median']} / NO {o['no_buy_median']})")
    print("\nper-city net flow (sell$ - buy$, crude realized proxy):")
    print(f"  {'city':<14}{'buy$':>9}{'sell$':>9}{'net$':>9}{'n':>6}")
    for c, d in list(o["city_flow"].items())[:16]:
        print(f"  {c:<14}{d['buy']:>9.0f}{d['sell']:>9.0f}{d['net']:>9.0f}{d['n']:>6}")
