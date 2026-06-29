"""Validation gate — does the strategy actually have edge, with no lookahead?

For each RESOLVED temperature event we:
  1. reconstruct the observed high-so-far at a chosen local decision hour using the
     Open-Meteo archive (obs <= that hour only — no peeking at the rest of the day);
  2. run the model to pick the favorite bucket and its probability;
  3. look up the ACTUAL market YES price of that bucket at the decision timestamp
     (CLOB prices-history);
  4. score the favorite against the REAL resolved winning bucket (ground truth =
     what paid out), and simulate buying it.

Ground truth is the resolved bucket, not our own weather read — so station mismatch
(we forecast Open-Meteo grid, market resolves Wunderground) is correctly punished.

Output: hit-rate, Brier, calibration, average edge vs market, and realized PnL.
"""
from __future__ import annotations
import json
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .clients import Polymarket, Weather, Metar
from .parse import build_event, TempEvent, Bucket
from .model import bucket_probs
from .config import COSTS, MODEL

# per-thread clients (requests.Session isn't guaranteed thread-safe to share)
_tl = threading.local()


def _clients():
    if not hasattr(_tl, "pm"):
        _tl.pm, _tl.wx, _tl.mt = Polymarket(), Weather(), Metar()
    return _tl.pm, _tl.wx, _tl.mt

# station obs (METAR) is the actual resolution source -> tight uncertainty:
# essentially just rounding + the small chance the high rises after decision hour.
METAR_SIGMA_C = 0.5


@dataclass
class Trial:
    city: str
    icao: Optional[str]
    day: str
    hour: int
    favorite: str
    fav_prob: float
    winner: Optional[str]
    hit: bool
    market_price: Optional[float]   # YES price of favorite at decision time
    traded: bool
    pnl: float                      # per $1 staked, net of friction


@dataclass
class NoTrial:
    """One dead-bucket NO-scalp: bought NO at the decision-hour price, held to resolution."""
    city: str
    day: str
    hour: int
    bucket: str
    margin_deg: float               # whole degrees the obs-high was above this bucket
    no_price: float                 # NO ask/price at decision time
    kind: str                       # SCALP (>=0.90) | STALE_DISCOUNT (<0.90)
    won: bool                       # did the bucket actually resolve NO (it should)
    pnl: float                      # payout(1 if won) - price - friction


def _winning_bucket(ev: TempEvent) -> Optional[Bucket]:
    """Resolved YES bucket = price_yes ~ 1 in the closed snapshot."""
    cand = [b for b in ev.buckets if b.price_yes >= 0.5]
    if not cand:
        return None
    return max(cand, key=lambda b: b.price_yes)


_PRICE_CACHE = Path(__file__).resolve().parent.parent / "data" / "pricecache"


def _price_at(pm: Polymarket, token_id: str, ts: int) -> Optional[float]:
    if not token_id:
        return None
    _PRICE_CACHE.mkdir(parents=True, exist_ok=True)
    fp = _PRICE_CACHE / f"{token_id[:24]}_{ts}.json"
    if fp.exists():
        hist = json.loads(fp.read_text())
    else:
        try:
            hist = pm.price_history(token_id, start_ts=ts - 6 * 3600,
                                    end_ts=ts + 3600, fidelity=10)
        except Exception:
            return None
        fp.write_text(json.dumps(hist))
    if not hist:
        return None
    best = min(hist, key=lambda h: abs(h.get("t", 0) - ts))
    return float(best.get("p")) if best.get("p") is not None else None


def _decision_ts(ev, H):
    """Local hour H on the event day, as UTC unix (tz-correct -> no lookahead)."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime(ev.day.year, ev.day.month, ev.day.day, H, tzinfo=ZoneInfo(ev.tz))
    except Exception:
        dt = datetime(ev.day.year, ev.day.month, ev.day.day, H, tzinfo=timezone.utc)
    return int(dt.timestamp())


def _obs_high(ev, H, source):
    pm, wx, mt = _clients()
    try:
        if source == "metar":
            return mt.high_so_far(ev.icao, ev.tz, ev.unit, ev.day, H) if ev.icao else None
        return wx.archive_high_so_far(ev.lat, ev.lon, ev.tz, ev.unit, ev.day, H)
    except Exception:
        return None


def _obs_low(ev, H, source):
    pm, wx, mt = _clients()
    try:
        if source == "metar" and ev.icao:
            return mt.low_so_far(ev.icao, ev.tz, ev.unit, ev.day, H)
    except Exception:
        return None
    return None


def _score_event(ev, hours, source="metar") -> List[Trial]:
    """YES-favorite strategy: pick the favorite bucket, buy if the book underprices it."""
    pm, wx, mt = _clients()
    winner = _winning_bucket(ev)
    if winner is None:
        return []
    out: List[Trial] = []
    for H in hours:
        floor = _obs_high(ev, H, source)
        if floor is None:
            continue
        sigma = (METAR_SIGMA_C * (1.8 if ev.unit == "F" else 1.0)) if source == "metar" else None
        mr = bucket_probs([floor], ev.buckets, floor=floor, unit=ev.unit, sigma=sigma)
        if mr is None:
            continue
        fav_b = next((b for b in ev.buckets if b.label == mr.favorite), None)
        price = _price_at(pm, fav_b.token_id if fav_b else "", _decision_ts(ev, H))
        hit = (mr.favorite == winner.label)
        traded, pnl = False, 0.0
        if price is not None and price <= COSTS.max_buy_price and (mr.fav_prob - price) >= COSTS.min_edge:
            traded = True
            cost = price + COSTS.friction_per_share
            pnl = (1.0 - cost) if hit else (-cost)
        out.append(Trial(
            city=ev.city, icao=ev.icao, day=ev.day.isoformat(), hour=H,
            favorite=mr.favorite, fav_prob=mr.fav_prob, winner=winner.label,
            hit=hit, market_price=price, traded=traded, pnl=pnl))
    return out


def _score_noscalp_event(ev, hours, source="metar",
                         margin: int = None) -> List[NoTrial]:
    """NO-scalp (the trader's real game): for every bucket the observed high has already
    passed by `margin`° at hour H, buy NO at that hour's price and hold to resolution.
    Ground truth: did the bucket actually resolve NO? (It should — a miss = station/
    resolution mismatch, the real tail risk.)"""
    pm, wx, mt = _clients()
    if margin is None:
        margin = COSTS.no_scalp_margin
    winner = _winning_bucket(ev)
    if winner is None:
        return []
    is_low = getattr(ev, "kind", "high") == "low"
    out: List[NoTrial] = []
    for H in hours:
        floor = _obs_low(ev, H, source) if is_low else _obs_high(ev, H, source)
        if floor is None:
            continue
        obs = round(floor)
        ts = _decision_ts(ev, H)
        for b in ev.buckets:
            if not b.no_token_id:
                continue
            if is_low:
                # LOW market: daily low <= obs; buckets ABOVE obs are dead -> NO
                if b.lo == float("-inf") or b.lo < obs + margin:
                    continue
                margin_deg = b.lo - obs
            else:
                # HIGH market: daily high >= obs; buckets BELOW obs are dead -> NO
                if b.hi == float("inf") or b.hi > obs - margin:
                    continue
                margin_deg = obs - b.hi
            no_price = _price_at(pm, b.no_token_id, ts)
            if no_price is None or no_price <= 0:
                continue
            won = (b.label != winner.label)       # NO wins unless this bucket was the high
            payout = 1.0 if won else 0.0
            out.append(NoTrial(
                city=ev.city + (" (low)" if is_low else ""), day=ev.day.isoformat(),
                hour=H, bucket=b.label,
                margin_deg=margin_deg, no_price=round(no_price, 4),
                kind="SCALP" if no_price >= COSTS.no_scalp_min_price else "STALE_DISCOUNT",
                won=won, pnl=round(payout - no_price - COSTS.friction_per_share, 4)))
    return out


def _summarize_noscalp(trials: List[NoTrial]) -> Dict:
    if not trials:
        return {"n": 0, "note": "no NO-scalp trials (need afternoon obs + NO price history)"}
    n = len(trials)
    won = sum(t.won for t in trials)
    scalp = [t for t in trials if t.kind == "SCALP"]
    stale = [t for t in trials if t.kind == "STALE_DISCOUNT"]
    by_margin: Dict[int, List[NoTrial]] = {}
    for t in trials:
        by_margin.setdefault(int(t.margin_deg), []).append(t)
    return {
        "n": n,
        "win_rate": won / n,                       # % dead buckets that resolved NO (safety!)
        "avg_entry": statistics.mean(t.no_price for t in trials),
        "total_pnl": sum(t.pnl for t in trials),
        "avg_pnl": statistics.mean(t.pnl for t in trials),
        "n_scalp": len(scalp), "n_stale": len(stale),
        "scalp_pnl": sum(t.pnl for t in scalp),
        "scalp_winrate": (sum(t.won for t in scalp) / len(scalp)) if scalp else None,
        "stale_pnl": sum(t.pnl for t in stale),
        "stale_winrate": (sum(t.won for t in stale) / len(stale)) if stale else None,
        "by_margin": {m: {"n": len(v), "win_rate": sum(t.won for t in v) / len(v),
                          "avg_pnl": statistics.mean(t.pnl for t in v)}
                      for m, v in sorted(by_margin.items())},
        "trials": trials,
    }


def run(events_limit: int = 60, hours: Optional[List[int]] = None,
        cities: Optional[List[str]] = None, universe: str = "trader",
        trader: Optional[str] = None, pages: int = 8, source: str = "metar",
        strategy: str = "favorite", workers: int = 8, verbose: bool = True,
        margin: Optional[int] = None, events: Optional[List] = None,
        auto_resolve: bool = False, include_low: bool = False) -> Dict:
    """Backtest on resolved events.
    source='metar' (actual station obs — viable) or 'grid' (Open-Meteo — ~48% ceiling).
    universe='trader' (the target's own played markets) or 'discover' (ALL resolved temp markets).
    strategy='favorite' (buy the YES favorite) or 'noscalp' (buy NO on dead buckets — the
    trader's real game; win_rate then measures the station/resolution-mismatch tail)."""
    hours = hours or [MODEL.peak_hour_local]
    if auto_resolve:                   # cover EVERY city Polymarket names (METAR)
        from . import parse as _parse
        _parse.AUTO_RESOLVE = True
    if events is None:
        events = _gather_events(universe, trader, events_limit, pages, cities)
    elif cities:                       # reuse a shared event list, filtered per-strategy
        events = [e for e in events if e.city in cities]
    if not include_low:                # default: high-temp markets only (unchanged)
        events = [e for e in events if getattr(e, "kind", "high") == "high"]
    if verbose:
        print(f"scoring {len(events)} resolved events x {len(hours)} hour(s) "
              f"[{strategy}/{source}]…")

    def score_one(ev):
        if strategy == "noscalp":
            return _score_noscalp_event(ev, hours, source, margin)
        return _score_event(ev, hours, source)
    trials: list = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(score_one, events):
            trials.extend(res)

    return _summarize_noscalp(trials) if strategy == "noscalp" else _summarize(trials)


def _gather_events(universe, trader, events_limit, pages, cities) -> List[TempEvent]:
    """Resolved TempEvents for the chosen universe (parallel fetch)."""
    from .recon import TARGET
    pm = Polymarket()
    raw_events: List[dict] = []
    if universe == "trader":
        acts = pm.activity(trader or TARGET, limit=1000)
        slugs, seen = [], set()
        for a in acts:
            s = a.get("eventSlug")
            if s and s not in seen:
                seen.add(s); slugs.append(s)

        def fetch(slug):
            p, _, _ = _clients()
            return p.event_by_slug(slug)
        with ThreadPoolExecutor(max_workers=8) as ex:
            raw_events = [e for e in ex.map(fetch, slugs) if e]
    else:
        raw_events = pm.temp_events(closed=True, pages=pages)

    out: List[TempEvent] = []
    for e in raw_events:
        ev = build_event(e)
        if ev is None or ev.day is None or len(ev.buckets) < 3 or not ev.closed:
            continue
        if cities and ev.city not in cities:
            continue
        out.append(ev)
        if len(out) >= events_limit:
            break
    return out


def _summarize(trials: List[Trial]) -> Dict:
    if not trials:
        return {"trials": [], "n": 0, "note": "no trials (data/archive gaps)"}
    n = len(trials)
    hits = sum(t.hit for t in trials)
    brier = statistics.mean((t.fav_prob - (1.0 if t.hit else 0.0)) ** 2 for t in trials)
    traded = [t for t in trials if t.traded]
    by_city: Dict[str, List[Trial]] = {}
    for t in trials:
        by_city.setdefault(t.city, []).append(t)

    summary = {
        "n": n,
        "hit_rate": hits / n,
        "brier": brier,
        "n_traded": len(traded),
        "trade_hit_rate": (sum(t.hit for t in traded) / len(traded)) if traded else None,
        "total_pnl": sum(t.pnl for t in traded),
        "avg_pnl_per_trade": (sum(t.pnl for t in traded) / len(traded)) if traded else None,
        "by_city": {c: {"n": len(v), "hit_rate": sum(t.hit for t in v) / len(v),
                         "pnl": sum(t.pnl for t in v if t.traded)}
                     for c, v in sorted(by_city.items(), key=lambda x: -len(x[1]))},
        "trials": trials,
    }
    return summary


def print_summary(s: Dict) -> None:
    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY (intraday-floor edge, resolved outcomes)")
    print("=" * 60)
    if not s.get("n"):
        print(s.get("note", "no data"))
        return
    print(f"events x hours scored : {s['n']}")
    print(f"favorite hit-rate     : {s['hit_rate']*100:.1f}%   (break-even ~88% to buy @0.88)")
    print(f"Brier score           : {s['brier']:.4f}   (lower=better; 0.25=coinflip)")
    print(f"trades taken          : {s['n_traded']}")
    if s["n_traded"]:
        print(f"  trade hit-rate      : {s['trade_hit_rate']*100:.1f}%")
        print(f"  total PnL ($/1 set) : {s['total_pnl']:+.2f}")
        print(f"  avg PnL per trade   : {s['avg_pnl_per_trade']:+.3f}")
    print("\nby city (n, hit-rate, traded PnL):")
    for c, d in s["by_city"].items():
        print(f"  {c:<14} n={d['n']:<3} hit={d['hit_rate']*100:5.1f}%  pnl={d['pnl']:+.2f}")
