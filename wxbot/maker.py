"""Maker strategy — the only configuration with a profitable precedent.

Two order types, both confined to the VERIFIED-station whitelist (data/verified_cities.json,
100% historical dead-call win-rate) and the safety gates:

  REWARD_BID  rest a NO BUY (>= rewardsMinSize shares, within rewardsMaxSpread of mid) on
              a "frontier" dead bucket — one the observed high has just passed. Earns the
              liquidity reward AND fills into convergence as the high climbs. (His game.)
  SWEEP       take an unusually cheap NO ASK (stale discount) on a margin-confirmed dead
              bucket — the rare big edge. Only with real depth.

Safety (every order): verified city · resolution day · dead by >= margin° · per-market and
total-exposure caps · price band. Routes to PaperBroker (default) or the env-gated LiveBroker.
Nothing here moves money on its own.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from .clients import Polymarket, Weather, Metar
from .parse import build_event, extract_icao
from .model import price_event_live
from .config import COSTS
from .execution import Order, RiskLimits

VERIFIED_FILE = Path(__file__).resolve().parent.parent / "data" / "verified_cities.json"
REWARD_MIN_SIZE = 50        # shares — Polymarket reward qualification (rewardsMinSize)
REWARD_MAX_SPREAD = 0.045   # within 4.5¢ of mid to qualify (rewardsMaxSpread)


def load_verified() -> set:
    try:
        return set(json.loads(VERIFIED_FILE.read_text()))
    except Exception:
        return set()


@dataclass
class MakerPlan:
    kind: str            # REWARD_BID | SWEEP
    city: str
    day: str
    bucket: str
    token_id: str        # NO token
    market_id: str
    side: str            # always BUY (we buy NO)
    price: float         # limit price
    size: float          # shares
    obs_high: float
    margin_deg: float
    est_edge: float      # to fair (1.00)
    reasons: List[str] = field(default_factory=list)

    def to_order(self) -> Order:
        return Order(token_id=self.token_id, side="BUY", price=self.price, size=self.size,
                     city=self.city, bucket=self.bucket, note=f"{self.kind} {self.est_edge:+.3f}")


def plan(stake_per: float = 50.0, max_orders: int = 6, bid_price: float = 0.97,
         sweep_max_ask: float = 0.90, min_depth_usd: float = 30.0,
         margin: Optional[int] = None, frontier_deg: int = 3,
         pages: int = 5) -> List[MakerPlan]:
    """Build the candidate maker orders. Pure planning — no orders are placed here."""
    verified = load_verified()
    if margin is None:
        margin = COSTS.no_scalp_margin
    pm, wx, mt = Polymarket(), Weather(), Metar()
    plans: List[MakerPlan] = []
    events = pm.temp_events(closed=False, pages=pages)

    for e in events:
        ev = build_event(e)
        if ev is None or ev.day is None or ev.city not in verified or not ev.icao:
            continue
        desc = (e.get("markets") or [{}])[0].get("description", "") or ""
        if extract_icao(desc) != ev.icao:        # station must come from the resolution URL
            continue
        try:
            now = datetime.now(ZoneInfo(ev.tz))
            if now.date() != ev.day or now.hour < 12:
                continue
        except Exception:
            continue
        try:
            mr, anchored = price_event_live(wx, mt, ev, {})
        except Exception:
            continue
        if not anchored or mr.floor is None:
            continue
        hi = round(mr.floor)

        dead = [b for b in ev.buckets
                if b.no_token_id and b.hi != float("inf") and b.hi <= hi - margin]
        books = pm.books([b.no_token_id for b in dead]) if dead else {}
        for b in dead:
            bk = books.get(b.no_token_id)
            if not bk:
                continue
            mdeg = hi - b.hi
            ask = bk.best_ask.price if bk.best_ask else None
            bid = bk.best_bid.price if bk.best_bid else None

            # SWEEP: a genuinely cheap NO on a confirmed-dead bucket, with real depth
            if ask is not None and ask <= sweep_max_ask:
                depth = ask * bk.best_ask.size
                if depth >= min_depth_usd:
                    sz = max(REWARD_MIN_SIZE, round(stake_per / ask, 2))
                    plans.append(MakerPlan(
                        kind="SWEEP", city=ev.city, day=ev.day.isoformat(), bucket=b.label,
                        token_id=b.no_token_id, market_id=b.market_id, side="BUY",
                        price=round(min(ask + 0.005, 0.99), 3), size=sz, obs_high=round(mr.floor, 1),
                        margin_deg=mdeg, est_edge=round(1.0 - ask, 3),
                        reasons=[f"cheap NO {ask:.3f} on dead bucket, depth ${depth:.0f}"]))
                continue

            # REWARD_BID: rest a NO bid on the frontier (just-dead) buckets to earn rewards
            # + fill into convergence. Skip already-pinned books (no room / no upside).
            if mdeg <= frontier_deg:
                mid = bk.mid or (bid if bid is not None else bid_price)
                if mid is not None and (mid - bid_price) > REWARD_MAX_SPREAD:
                    # our bid would sit outside the reward band; pull it toward mid
                    px = round(mid - REWARD_MAX_SPREAD + 0.001, 3)
                else:
                    px = bid_price
                px = min(px, 0.985)
                plans.append(MakerPlan(
                    kind="REWARD_BID", city=ev.city, day=ev.day.isoformat(), bucket=b.label,
                    token_id=b.no_token_id, market_id=b.market_id, side="BUY",
                    price=px, size=REWARD_MIN_SIZE, obs_high=round(mr.floor, 1),
                    margin_deg=mdeg, est_edge=round(1.0 - px, 3),
                    reasons=[f"frontier dead (margin {mdeg}°), rest NO@{px} for reward+convergence"]))

    # best edge first; bound the number of orders for a small test
    plans.sort(key=lambda p: (-{"SWEEP": 1, "REWARD_BID": 0}[p.kind], -p.est_edge))
    return plans[:max_orders], len(events)


def _broker(live: bool, stake_per: float, total_cap: float):
    from .execution import PaperBroker, LiveBroker
    limits = RiskLimits(max_stake_per_market=stake_per * 1.05, max_total_exposure=total_cap)
    return (LiveBroker(limits) if live else PaperBroker(limits=limits))


def run(live: bool = False, stake_per: float = 50.0, max_orders: int = 6,
        total_cap: float = 250.0, verbose: bool = True) -> dict:
    """Place maker orders fresh (no reconcile). live=False -> PaperBroker (dry-run)."""
    broker = _broker(live, stake_per, total_cap)
    plans, n_events = plan(stake_per=stake_per, max_orders=max_orders)
    if verbose:
        mode = "LIVE" if live else "PAPER (dry-run)"
        print(f"[{mode}] {len(plans)} maker order(s) planned ({n_events} events scanned, "
              f"verified cities only, caps ${stake_per}/mkt, ${total_cap} total):")
    results = []
    for p in plans:
        rec = broker.place(p.to_order(), post_only=(p.kind == "REWARD_BID"))
        results.append({"plan": p.kind, "city": p.city, "bucket": p.bucket,
                        "price": p.price, "size": p.size, "status": rec.get("status"),
                        "reason": rec.get("reason")})
        if verbose:
            print(f"  {p.kind:<10} {p.city:<12} {p.bucket:<14} NO@{p.price} x{p.size:.0f} "
                  f"edge{p.est_edge:+.3f} -> {rec.get('status')} {rec.get('reason') or ''}")
    return {"planned": len(plans), "results": results}


def reconcile(live: bool = False, stake_per: float = 50.0, max_orders: int = 6,
              total_cap: float = 250.0, price_tol: float = 0.005, verbose: bool = True) -> dict:
    """Bring resting orders in line with the current plan: cancel orders that are no
    longer wanted (bucket resolved / no longer frontier / price drifted) and place the
    ones missing. Skips ALL cancels if the scan returned no events (transient failure
    guard) so we never mass-cancel good orders on a hiccup."""
    broker = _broker(live, stake_per, total_cap)
    plans, n_events = plan(stake_per=stake_per, max_orders=max_orders)
    if n_events == 0:
        if verbose:
            print("scan returned 0 events — skipping reconcile (no cancellations).")
        return {"skipped": True}
    desired = {p.token_id: p for p in plans}
    open_orders = broker.open_orders()
    cancelled = placed = kept = 0
    open_by_tok = {}
    for o in open_orders:
        d = desired.get(o["token_id"])
        if d is None or abs(o["price"] - d.price) > price_tol:
            broker.cancel(o["id"]); cancelled += 1
        else:
            open_by_tok[o["token_id"]] = o; kept += 1
    for p in plans:
        if p.token_id not in open_by_tok:
            r = broker.place(p.to_order(), post_only=(p.kind == "REWARD_BID")); placed += 1
            if verbose:
                print(f"  +place {p.kind:<10} {p.city:<12} {p.bucket:<14} NO@{p.price} "
                      f"x{p.size:.0f} -> {r.get('status')} {r.get('reason') or ''}")
    if verbose:
        mode = "LIVE" if live else "PAPER"
        print(f"[{mode}] reconcile: {kept} kept · {cancelled} cancelled · {placed} placed "
              f"(desired {len(plans)}, open_before {len(open_orders)}, {n_events} events)")
    return {"kept": kept, "cancelled": cancelled, "placed": placed,
            "desired": len(plans), "open_before": len(open_orders)}


def cancel_all(live: bool = False) -> dict:
    """Kill switch — cancel every resting order."""
    broker = _broker(live, 50.0, 250.0)
    r = broker.cancel_all()
    print(f"[{'LIVE' if live else 'PAPER'}] cancel_all -> {r}")
    return r
