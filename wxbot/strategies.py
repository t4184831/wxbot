"""Named weather-betting strategies + a comparison runner over the backtest engine.

Each strategy is a set of backtest.run() kwargs; compare() runs them all on the
same resolved-event universe so you can rank by realized PnL / hit-rate. The model
is most accurate LATE in the local day (the high is locked in) and on STABLE-climate
cities; the NO-scalp is the higher-volume play and is safer with a bigger °-margin.
"""
from __future__ import annotations

from .backtest import run
from .config import PREFERRED_CITIES

STRATEGIES: dict[str, dict] = {
    # --- YES favorite: buy the model's favorite bucket when the book underprices it ---
    "favorite_h17":            dict(strategy="favorite", hours=[17]),
    "favorite_late_h19":       dict(strategy="favorite", hours=[19]),       # later = high more locked
    "favorite_stable_cities":  dict(strategy="favorite", hours=[17], cities=PREFERRED_CITIES),
    # --- NO-scalp: buy NO on buckets the observed high already passed (the volume play) ---
    "noscalp_margin1":         dict(strategy="noscalp", hours=[14, 17, 20], margin=1),
    "noscalp_margin2_safe":    dict(strategy="noscalp", hours=[14, 17, 20], margin=2),
    "noscalp_late_margin2":    dict(strategy="noscalp", hours=[18, 20], margin=2),
}


def compare(events_limit: int = 50, universe: str = "trader", source: str = "metar",
            workers: int = 8, on_result=None) -> dict:
    """Gather the resolved-event universe ONCE, then score every preset against it
    (price-history is cached after the first pass). `on_result(name, summary)` is
    called as each strategy finishes — use it to stream progress."""
    from .backtest import _gather_events
    shared = _gather_events(universe, None, events_limit, 8, None)   # gather once, no city filter
    out = {}
    for name, kw in STRATEGIES.items():
        kw = dict(kw)
        kw.pop("cities", None)                      # city filter applied to `shared` inside run()
        cities = STRATEGIES[name].get("cities")
        out[name] = run(events=shared, cities=cities, source=source,
                        workers=workers, verbose=False, **kw)
        if on_result:
            on_result(name, out[name])
    return out


def summarize_row(name: str, s: dict) -> dict:
    """Flatten a strategy summary into a comparison row (handles both the favorite
    and noscalp summary shapes)."""
    if "hit_rate" in s:                                   # favorite-style summary
        return {"strategy": name, "type": "favorite", "events": s.get("n", 0),
                "hit%": round(s.get("hit_rate", 0) * 100, 1),
                "trades": s.get("n_traded", 0),
                "PnL$": round(s.get("total_pnl", 0) or 0, 2),
                "avgPnL": round(s.get("avg_pnl_per_trade") or 0, 3)}
    return {"strategy": name, "type": "noscalp", "events": s.get("n", 0),       # noscalp-style
            "hit%": round((s.get("win_rate") or 0) * 100, 1),
            "trades": s.get("n", 0),
            "PnL$": round(s.get("total_pnl") or 0, 2),
            "avgPnL": round(s.get("avg_pnl") or 0, 4)}


def comparison_table(results: dict) -> list[dict]:
    rows = [summarize_row(n, s) for n, s in results.items()]
    rows.sort(key=lambda r: r["PnL$"], reverse=True)
    return rows
