"""Live edge scanner: find open temperature buckets where the model's probability
beats the order-book ask by more than our friction + edge threshold.

Mirrors the target trader: anchor on the resolution station's live METAR (the data
the market resolves on), favor the high-probability favorite bucket, only buy below
max_buy_price, and prefer stable/thin cities. Optionally routes signals to the
PaperBroker (no real money).
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .clients import Polymarket, Weather, Metar
from .parse import build_event
from .model import price_event_live
from .config import COSTS, MODEL, PREFERRED_CITIES


@dataclass
class Signal:
    city: str
    day: str
    bucket: str
    model_prob: float
    best_ask: Optional[float]
    edge: float
    ask_size: float
    anchored: bool          # True = METAR-anchored (high observable) vs grid-only
    preferred: bool
    token_id: str
    event_id: str = ""      # for settling against the real resolution later
    market_id: str = ""
    side: str = "YES"       # YES (buy favorite) or NO (scalp a dead bucket)

    def __str__(self):
        a = f"{self.best_ask:.3f}" if self.best_ask is not None else " na "
        star = "*" if self.preferred else " "
        src = "M" if self.anchored else "g"   # M=METAR-anchored, g=grid-only
        return (f"{star}[{src}] {self.side:<3} {self.city:<12} {self.day} {self.bucket:<14} "
                f"p={self.model_prob:.3f} ask={a} edge={self.edge:+.3f} "
                f"sz={self.ask_size:.0f}")


def _load_biases() -> Dict[str, float]:
    """Per-station OM-vs-resolution bias learned from resolved history (if present)."""
    try:
        from .calibrate import load_dataset, station_bias
        rows = load_dataset()
        return station_bias(rows) if rows else {}
    except Exception:
        return {}


def scan(min_edge: Optional[float] = None, preferred_only: bool = False,
         anchored_only: bool = True, paper: bool = False,
         stake: float = 20.0, verbose: bool = True) -> List[Signal]:
    """anchored_only: only emit signals where we have a live METAR anchor (the
    validated regime). paper=True routes each signal to the PaperBroker."""
    pm, wx, mt = Polymarket(), Weather(), Metar()
    biases = _load_biases()
    min_edge = COSTS.min_edge if min_edge is None else min_edge
    events = pm.temp_events(closed=False, pages=6)
    signals: List[Signal] = []

    for e in events:
        ev = build_event(e)
        if ev is None or ev.day is None or len(ev.buckets) < 3:
            continue
        if preferred_only and ev.city not in PREFERRED_CITIES:
            continue
        try:
            mr, anchored = price_event_live(wx, mt, ev, biases)
        except Exception:
            continue
        if mr is None or mr.fav_prob < MODEL.min_fav_prob:
            continue
        if anchored_only and not anchored:
            continue
        # price every high-prob bucket against the live book
        tokens = [b.token_id for b in ev.buckets if mr.prob_of(b.label) >= 0.15 and b.token_id]
        books = pm.books(tokens) if tokens else {}
        for b in ev.buckets:
            p = mr.prob_of(b.label)
            if p < MODEL.min_fav_prob:
                continue
            bk = books.get(b.token_id)
            ask = bk.best_ask if bk else None
            # require a real, two-sided book — a lone dust ask is a stale/dead market
            if ask is None or bk.best_bid is None:
                continue
            # the market must already see this as a plausible contender, and not be
            # priced too rich. Buying 0.001 longshots on model overconfidence = the trap.
            if not (COSTS.min_buy_price <= ask.price <= COSTS.max_buy_price):
                continue
            edge = p - ask.price - COSTS.friction_per_share
            # a gigantic "edge" means we disagree with the whole market -> we're wrong
            if min_edge <= edge <= COSTS.max_edge:
                signals.append(Signal(
                    city=ev.city, day=ev.day.isoformat(), bucket=b.label,
                    model_prob=p, best_ask=ask.price, edge=edge,
                    ask_size=ask.size, anchored=anchored,
                    preferred=ev.city in PREFERRED_CITIES, token_id=b.token_id,
                    event_id=ev.event_id, market_id=b.market_id, side="YES"))

        # --- NO-scalp: buy NO on buckets the observed high has ALREADY passed ---
        # (the trader's main game). These are factually dead -> NO ~ 1.00; harvest
        # the last cents. Requires the METAR floor (observed high) and a margin.
        if anchored and mr.floor is not None:
            hi_obs = round(mr.floor)
            dead = [b for b in ev.buckets
                    if b.no_token_id and b.hi <= hi_obs - COSTS.no_scalp_margin
                    and mr.prob_of(b.label) < 0.02]
            nob = pm.books([b.no_token_id for b in dead]) if dead else {}
            for b in dead:
                bk = nob.get(b.no_token_id)
                ask = bk.best_ask if bk else None
                if ask is None or bk.best_bid is None:        # need a two-sided book
                    continue
                if not (COSTS.no_scalp_min_price <= ask.price <= COSTS.no_scalp_max_price):
                    continue
                edge = 1.0 - ask.price - COSTS.friction_per_share  # converges to 1.00
                if edge >= COSTS.no_scalp_min_edge:
                    signals.append(Signal(
                        city=ev.city, day=ev.day.isoformat(), bucket=b.label,
                        model_prob=1.0 - mr.prob_of(b.label), best_ask=ask.price,
                        edge=edge, ask_size=ask.size, anchored=anchored,
                        preferred=ev.city in PREFERRED_CITIES, token_id=b.no_token_id,
                        event_id=ev.event_id, market_id=b.market_id, side="NO"))

    signals.sort(key=lambda s: (s.preferred, s.edge), reverse=True)
    if verbose:
        tag = "METAR-anchored only" if anchored_only else "all"
        print(f"\n{len(signals)} signals ({tag}, min_edge={min_edge}):")
        for s in signals:
            print("  " + str(s))

    if paper and signals:
        _paper_trade(pm, signals, stake, verbose)
    return signals


def _paper_trade(pm: Polymarket, signals: List[Signal], stake: float, verbose: bool):
    from .execution import PaperBroker, Order
    broker = PaperBroker(pm)
    if verbose:
        print(f"\n--- paper-trading {len(signals)} signals (${stake} each) ---")
    for s in signals:
        if s.best_ask is None or s.best_ask <= 0:
            continue
        shares = round(stake / s.best_ask, 2)
        rec = broker.submit(Order(
            token_id=s.token_id, side="BUY", price=round(s.best_ask + 0.01, 3),
            size=shares, city=s.city, bucket=s.bucket, note=f"edge={s.edge:+.3f}"))
        if verbose:
            print(f"  {rec['status']:<14} {s.city:<12} {s.bucket:<14} "
                  f"{rec.get('reason') or 'fill@%.3f stake$%.2f' % (rec.get('fill_price',0), rec.get('stake',0))}")
