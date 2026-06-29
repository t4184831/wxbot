"""Polymarket REST client: Gamma (metadata) + CLOB (books/history) + Data API
(trades/positions) + PnL API. Read-only; trading lives in execution.py.

Adapted from the sibling pmarb project's client, trimmed to temperature markets.
"""
from __future__ import annotations
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

import requests
from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

from ..config import GAMMA, CLOB, DATA_API, PNL_API, HEADERS


def make_session(headers: dict, retries: int = 4) -> requests.Session:
    """Session with backoff retries — these public APIs flake intermittently."""
    s = requests.Session()
    s.headers.update(headers)
    r = Retry(total=retries, connect=retries, read=retries, backoff_factor=0.8,
              status_forcelist=(429, 500, 502, 503, 504),
              allowed_methods=frozenset(["GET", "POST"]))
    ad = HTTPAdapter(max_retries=r, pool_connections=20, pool_maxsize=20)
    s.mount("https://", ad)
    s.mount("http://", ad)
    return s


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Level:
    price: float
    size: float


@dataclass
class Book:
    token_id: str
    bids: List[Level]   # buyers (you SELL into best bid)
    asks: List[Level]   # sellers (you BUY at best ask)
    ts: float

    @property
    def best_bid(self) -> Optional[Level]:
        return max(self.bids, key=lambda l: l.price) if self.bids else None

    @property
    def best_ask(self) -> Optional[Level]:
        return min(self.asks, key=lambda l: l.price) if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        return (bb.price + ba.price) / 2 if bb and ba else None

    def cost_to_buy(self, shares: float) -> Optional[float]:
        return _walk(sorted(self.asks, key=lambda l: l.price), shares)

    def proceeds_to_sell(self, shares: float) -> Optional[float]:
        return _walk(sorted(self.bids, key=lambda l: -l.price), shares)


def _walk(levels: List[Level], shares: float) -> Optional[float]:
    need, cost = shares, 0.0
    for lv in levels:
        take = min(need, lv.size)
        cost += take * lv.price
        need -= take
        if need <= 1e-9:
            return cost / shares
    return None


@dataclass
class Market:
    id: str
    question: str
    slug: str
    condition_id: str


class Polymarket:
    def __init__(self, timeout: float = 40.0):
        self.s = make_session(HEADERS)
        self.timeout = timeout

    # ---------- Gamma: temperature event discovery ----------
    def events(self, **params) -> List[dict]:
        params.setdefault("limit", 200)
        r = self.s.get(f"{GAMMA}/events", params=params, timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        return d if isinstance(d, list) else d.get("data", [])

    def temp_events(self, closed: bool = False, pages: int = 6, page_size: int = 200,
                    order: str = "volume24hr") -> List[dict]:
        """Sweep events and keep those whose title is a daily-temperature market."""
        out, seen = [], set()
        for off in range(0, pages * page_size, page_size):
            try:
                raw = self.events(closed=str(closed).lower(), limit=page_size,
                                  offset=off, order=order, ascending="false")
            except requests.HTTPError:
                break
            if not raw:
                break
            for e in raw:
                t = (e.get("title") or "").lower()
                if "temperature in" in t and e.get("id") not in seen:
                    seen.add(e.get("id"))
                    out.append(e)
        return out

    def event(self, event_id: str) -> Optional[dict]:
        try:
            r = self.s.get(f"{GAMMA}/events/{event_id}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError:
            res = self.events(id=event_id, limit=1)
            return res[0] if res else None

    def event_by_slug(self, slug: str) -> Optional[dict]:
        res = self.events(slug=slug, limit=1)
        return res[0] if res else None

    # ---------- CLOB: books + history ----------
    def book(self, token_id: str) -> Book:
        r = self.s.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=self.timeout)
        r.raise_for_status()
        return self._parse_book(token_id, r.json())

    def books(self, token_ids: Iterable[str]) -> dict:
        ids = [t for t in token_ids if t]
        try:
            r = self.s.post(f"{CLOB}/books", json=[{"token_id": t} for t in ids],
                            timeout=self.timeout)
            r.raise_for_status()
            out = {}
            for entry in r.json():
                tid = str(entry.get("asset_id") or entry.get("token_id"))
                out[tid] = self._parse_book(tid, entry)
            for t in ids:
                out.setdefault(t, self.book(t))
            return out
        except requests.RequestException:
            return {t: self.book(t) for t in ids}

    def _parse_book(self, token_id: str, d: dict) -> Book:
        def lv(side):
            return [Level(float(x["price"]), float(x["size"])) for x in d.get(side, []) or []]
        return Book(token_id=token_id, bids=lv("bids"), asks=lv("asks"), ts=time.time())

    def price_history(self, token_id: str, start_ts: Optional[int] = None,
                      end_ts: Optional[int] = None, interval: Optional[str] = None,
                      fidelity: Optional[int] = None) -> List[dict]:
        """[{t: unix_sec, p: price}]. Use interval='1h'|'1d'|'max' OR start/end."""
        params: dict = {"market": token_id}
        if interval:
            params["interval"] = interval
        if start_ts:
            params["startTs"] = start_ts
        if end_ts:
            params["endTs"] = end_ts
        if fidelity:
            params["fidelity"] = fidelity
        r = self.s.get(f"{CLOB}/prices-history", params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json().get("history", [])

    # ---------- Data API: trader recon ----------
    def activity(self, user: str, limit: int = 1000, type_: str = "TRADE") -> List[dict]:
        r = self.s.get(f"{DATA_API}/activity", params={
            "user": user, "limit": limit, "sortBy": "TIMESTAMP",
            "sortDirection": "DESC", "type": type_}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def positions(self, user: str, limit: int = 500) -> List[dict]:
        r = self.s.get(f"{DATA_API}/positions", params={
            "user": user, "limit": limit, "sortBy": "CURRENT",
            "sortDirection": "DESC"}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def user_pnl(self, user: str, interval: str = "all", fidelity: str = "1d") -> List[dict]:
        r = self.s.get(f"{PNL_API}/user-pnl", params={
            "user_address": user, "interval": interval, "fidelity": fidelity},
            timeout=self.timeout)
        r.raise_for_status()
        return r.json()
