"""Opportunity engine — the brain behind the dashboard.

Scans open temperature markets and classifies every actionable edge into one of:

  SCALP          buy NO on a bucket the observed high has already passed; NO ~0.97-0.99,
                 harvest the last cents to 1.00 (the trader's bread-and-butter).
  STALE_DISCOUNT same dead bucket, but a stale/illiquid book leaves NO cheap (<0.90) ->
                 huge edge IF the bucket is truly dead. Treated with suspicion.
  FAVORITE       buy YES on the model's favorite bucket when the book underprices it.

Each opportunity carries the three safety checks that decide whether it is real:
  station_ok  the resolution ICAO was extracted from the market's own Wunderground URL
  on_day      the market's day == today in the station's local timezone
  margin_ok   (NO side) the bucket is dead by >= no_scalp_margin whole degrees
"""
from __future__ import annotations
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from .clients import Polymarket, Weather, Metar
from .parse import build_event, extract_icao
from .model import price_event_live
from .config import COSTS, PREFERRED_CITIES

_WU = re.compile(r"https?://www\.wunderground\.com/\S+")


@dataclass
class Opportunity:
    city: str
    day: str
    icao: Optional[str]
    tz: str
    unit: str
    kind: str            # SCALP | STALE_DISCOUNT | FAVORITE
    side: str            # NO | YES
    bucket: str
    obs_high: Optional[float]
    local_hour: Optional[int]
    bid: Optional[float]
    ask: Optional[float]
    room_cents: Optional[float]   # cents from ask to 1.00 (NO) / model edge basis
    depth_usd: float
    model_prob: float             # P(this side resolves YES) per our data
    edge: float                   # expected $/share to fair (1.00 for dead NO)
    margin_deg: Optional[float]   # whole degrees the high is above the bucket (NO)
    station_ok: bool
    on_day: bool
    margin_ok: bool
    preferred: bool
    verdict: str                  # TRADEABLE | WATCH | BLOCKED:<reason>
    reasons: List[str] = field(default_factory=list)
    wunderground: Optional[str] = None
    token_id: str = ""
    market_id: str = ""
    event_id: str = ""

    def to_row(self) -> dict:
        return asdict(self)


def _verdict(o: "Opportunity") -> "Opportunity":
    r = []
    if not o.station_ok:
        r.append("station not confirmed from resolution URL")
    if not o.on_day:
        r.append("not the resolution day yet")
    if o.side == "NO" and not o.margin_ok:
        r.append("bucket not dead by safe margin")
    if o.bid is None:
        r.append("one-sided/stale book")
    if o.kind == "STALE_DISCOUNT":
        r.append("price too good — verify Wunderground == METAR before trusting")
    o.reasons = r
    if not o.station_ok or not o.on_day or (o.side == "NO" and not o.margin_ok):
        o.verdict = "BLOCKED"
    elif o.kind == "STALE_DISCOUNT" or o.bid is None:
        o.verdict = "WATCH"
    else:
        o.verdict = "TRADEABLE"
    return o


# per-thread clients (requests.Session isn't guaranteed thread-safe to share)
_tl = threading.local()


def _clients():
    if not hasattr(_tl, "pm"):
        _tl.pm, _tl.wx, _tl.mt = Polymarket(), Weather(), Metar()
    return _tl.pm, _tl.wx, _tl.mt


def find_opportunities(pages: int = 5, include_favorites: bool = True,
                       only_afternoon: bool = True, workers: int = 8) -> List[Opportunity]:
    try:
        from .calibrate import load_dataset, station_bias
        rows = load_dataset()
        biases = station_bias(rows) if rows else {}
    except Exception:
        biases = {}

    events = Polymarket().temp_events(closed=False, pages=pages)
    out: List[Opportunity] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for res in ex.map(lambda e: _process_event(e, biases, include_favorites,
                                                   only_afternoon), events):
            out.extend(res)

    rank = {"TRADEABLE": 0, "WATCH": 1, "BLOCKED": 2}
    out.sort(key=lambda o: (rank.get(o.verdict, 3), -o.edge))
    return out


def _process_event(e: dict, biases: dict, include_favorites: bool,
                   only_afternoon: bool) -> List[Opportunity]:
    pm, wx, mt = _clients()
    out: List[Opportunity] = []
    ev = build_event(e)
    if ev is None or ev.day is None or len(ev.buckets) < 3:
        return out
    desc = (e.get("markets") or [{}])[0].get("description", "") or e.get("description", "") or ""
    url_icao = extract_icao(desc)
    station_ok = bool(url_icao) and (url_icao == ev.icao)
    wu = _WU.search(desc)
    wu_url = wu.group(0).rstrip(").,") if wu else None
    try:
        now = datetime.now(ZoneInfo(ev.tz))
        local_hour, on_day = now.hour, now.date() == ev.day
    except Exception:
        local_hour, on_day = None, False
    if only_afternoon and (local_hour is None or local_hour < 12 or not on_day):
        return out

    try:
        mr, anchored = price_event_live(wx, mt, ev, biases)
    except Exception:
        return out
    if mr is None:
        return out
    obs_high = mr.floor if anchored else None
    pref = ev.city in PREFERRED_CITIES

    if True:
        # ---- NO-side: dead buckets ----
        if obs_high is not None:
            hi_obs = round(obs_high)
            dead = [b for b in ev.buckets
                    if b.no_token_id and b.hi != float("inf") and b.hi < hi_obs]
            books = pm.books([b.no_token_id for b in dead]) if dead else {}
            for b in dead:
                bk = books.get(b.no_token_id)
                if not bk or not bk.best_ask:
                    continue
                ask = bk.best_ask.price
                bid = bk.best_bid.price if bk.best_bid else None
                margin = hi_obs - b.hi
                kind = "SCALP" if ask >= COSTS.no_scalp_min_price else "STALE_DISCOUNT"
                o = Opportunity(
                    city=ev.city, day=ev.day.isoformat(), icao=ev.icao, tz=ev.tz,
                    unit=ev.unit, kind=kind, side="NO", bucket=b.label,
                    obs_high=round(obs_high, 1), local_hour=local_hour, bid=bid, ask=ask,
                    room_cents=round((1.0 - ask) * 100, 2),
                    depth_usd=round(ask * bk.best_ask.size, 0),
                    model_prob=round(1.0 - mr.prob_of(b.label), 4),
                    edge=round(1.0 - ask - COSTS.friction_per_share, 4),
                    margin_deg=margin, station_ok=station_ok, on_day=on_day,
                    margin_ok=margin >= COSTS.no_scalp_margin, preferred=pref,
                    verdict="", wunderground=wu_url, token_id=b.no_token_id,
                    market_id=b.market_id, event_id=ev.event_id)
                out.append(_verdict(o))

        # ---- YES-side: favorite ----
        if include_favorites and mr.fav_prob >= 0.80:
            fb = next((b for b in ev.buckets if b.label == mr.favorite), None)
            if fb and fb.token_id:
                bk = pm.book(fb.token_id)
                if bk.best_ask:
                    ask = bk.best_ask.price
                    bid = bk.best_bid.price if bk.best_bid else None
                    edge = round(mr.fav_prob - ask - COSTS.friction_per_share, 4)
                    if COSTS.min_buy_price <= ask <= COSTS.max_buy_price and edge >= COSTS.min_edge:
                        o = Opportunity(
                            city=ev.city, day=ev.day.isoformat(), icao=ev.icao, tz=ev.tz,
                            unit=ev.unit, kind="FAVORITE", side="YES", bucket=fb.label,
                            obs_high=None if obs_high is None else round(obs_high, 1),
                            local_hour=local_hour, bid=bid, ask=ask,
                            room_cents=round(edge * 100, 2),
                            depth_usd=round(ask * bk.best_ask.size, 0) if bk.best_ask else 0,
                            model_prob=round(mr.fav_prob, 4), edge=edge, margin_deg=None,
                            station_ok=station_ok, on_day=on_day, margin_ok=True,
                            preferred=pref, verdict="", wunderground=wu_url,
                            token_id=fb.token_id, market_id=fb.market_id, event_id=ev.event_id)
                        out.append(_verdict(o))

    return out
