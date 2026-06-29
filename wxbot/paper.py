"""Resolution-marked paper tracker — Gate A.

Records each scanner signal as an OPEN paper position (conservative taker entry at
the ask + friction), then later SETTLES it against the market's ACTUAL resolution
(did that bucket win?) to produce an honest, no-money paper track record.

This answers the only question that matters before risking capital: do capturable,
plausibly-priced signals actually appear in the live afternoon windows, and is the
realized PnL positive after we mark them to truth?

Note: this is the TAKER view (we buy an existing ask). Maker fills can't be
simulated — that needs Gate B (small real money).
"""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .clients import Polymarket
from .config import COSTS

BOOK = Path(__file__).resolve().parent.parent / "data" / "paper_positions.json"


@dataclass
class Position:
    ts: float
    city: str
    day: str
    bucket: str
    model_prob: float
    entry: float            # cost per share (ask + friction)
    shares: float
    stake: float
    token_id: str
    event_id: str
    market_id: str
    anchored: bool
    side: str = "YES"       # YES (favorite) or NO (dead-bucket scalp)
    status: str = "OPEN"    # OPEN | WON | LOST | VOID
    payout: Optional[float] = None
    pnl: Optional[float] = None


def _load() -> List[dict]:
    return json.loads(BOOK.read_text()) if BOOK.exists() else []


def _save(rows: List[dict]) -> None:
    BOOK.parent.mkdir(parents=True, exist_ok=True)
    BOOK.write_text(json.dumps(rows, indent=2))


def record(signals, stake: float = 20.0, anchored_only: bool = True) -> int:
    """Add new OPEN positions (deduped by token_id+day). Returns count added."""
    rows = _load()
    have = {(r["token_id"], r["day"]) for r in rows}
    added = 0
    for s in signals:
        if anchored_only and not s.anchored:
            continue
        if s.best_ask is None or s.best_ask <= 0:
            continue
        key = (s.token_id, s.day)
        if key in have:
            continue
        entry = round(s.best_ask + COSTS.friction_per_share, 4)
        shares = round(stake / entry, 2)
        rows.append(asdict(Position(
            ts=datetime.now(timezone.utc).timestamp(), city=s.city, day=s.day,
            bucket=s.bucket, model_prob=round(s.model_prob, 4), entry=entry,
            shares=shares, stake=round(entry * shares, 2), token_id=s.token_id,
            event_id=s.event_id, market_id=s.market_id, anchored=s.anchored,
            side=getattr(s, "side", "YES"))))
        have.add(key)
        added += 1
    _save(rows)
    return added


def settle(pm: Optional[Polymarket] = None, verbose: bool = True) -> int:
    """Mark matured OPEN positions to their real resolution. Returns count settled."""
    pm = pm or Polymarket()
    rows = _load()
    today = datetime.now(timezone.utc).date()
    settled = 0
    ev_cache: Dict[str, dict] = {}
    for r in rows:
        if r["status"] != "OPEN":
            continue
        if date.fromisoformat(r["day"]) >= today:
            continue  # not over yet (give it the full day + resolution lag)
        ev = ev_cache.get(r["event_id"])
        if ev is None:
            ev = pm.event(r["event_id"]) or {}
            ev_cache[r["event_id"]] = ev
        mkts = ev.get("markets", []) or []
        mk = next((m for m in mkts if str(m.get("id")) == r["market_id"]), None)
        if not mk or not mk.get("closed"):
            continue  # not resolved yet
        prices = mk.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except json.JSONDecodeError:
                prices = None
        if not prices:
            continue
        yes_won = float(prices[0]) >= 0.5
        won = yes_won if r.get("side", "YES") == "YES" else (not yes_won)
        r["payout"] = round(r["shares"] * (1.0 if won else 0.0), 2)
        r["pnl"] = round(r["payout"] - r["stake"], 2)
        r["status"] = "WON" if won else "LOST"
        settled += 1
        if verbose:
            print(f"  settled {r['city']:<12} {r['day']} {r['bucket']:<14} "
                  f"{r['status']:<4} pnl={r['pnl']:+.2f}")
    _save(rows)
    return settled


def report() -> Dict:
    rows = _load()
    closed = [r for r in rows if r["status"] in ("WON", "LOST")]
    open_ = [r for r in rows if r["status"] == "OPEN"]
    wins = [r for r in closed if r["status"] == "WON"]
    total_stake = sum(r["stake"] for r in closed)
    total_pnl = sum(r["pnl"] or 0 for r in closed)
    out = {
        "open": len(open_), "settled": len(closed),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "total_stake": round(total_stake, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": (total_pnl / total_stake) if total_stake else None,
    }
    print("\n=== PAPER TRACK RECORD ===")
    print(f"open positions : {out['open']}")
    print(f"settled        : {out['settled']}")
    if closed:
        print(f"win rate       : {out['win_rate']*100:.1f}%")
        print(f"staked         : ${out['total_stake']:.2f}")
        print(f"realized PnL   : ${out['total_pnl']:+.2f}  (ROI {out['roi']*100:+.1f}%)")
    else:
        print("(no settled positions yet — let the afternoon windows accumulate)")
    return out
