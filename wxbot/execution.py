"""Execution layer.

PaperBroker  — default. Simulates fills against the live order book and logs to
               data/paper_trades.jsonl. Risk controls applied. Zero money at risk.
LiveBroker   — real CLOB orders via py-clob-client. HARD-DISABLED unless you set
               WXBOT_LIVE=1 and provide POLY_PK / POLY_FUNDER. Even then it places
               LIMIT orders only, capped by the same risk controls.

This file never trades on its own. You run it, with your own funded wallet, after
the backtest clears the >88% bar on paper. Nothing here moves money implicitly.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from .clients import Polymarket
from .config import COSTS

DATA = Path(__file__).resolve().parent.parent / "data"
DATA.mkdir(exist_ok=True)


@dataclass
class RiskLimits:
    max_stake_per_market: float = 50.0     # $ per bucket
    max_total_exposure: float = 500.0      # $ across all open paper positions
    max_buy_price: float = COSTS.max_buy_price


@dataclass
class Order:
    token_id: str
    side: str          # "BUY" / "SELL"
    price: float       # limit price
    size: float        # shares
    city: str = ""
    bucket: str = ""
    note: str = ""


class PaperBroker:
    def __init__(self, pm: Optional[Polymarket] = None, limits: Optional[RiskLimits] = None,
                 log_path: Optional[Path] = None):
        self.pm = pm or Polymarket()
        self.limits = limits or RiskLimits()
        self.log_path = log_path or (DATA / "paper_trades.jsonl")
        self.exposure = 0.0

    def _check(self, o: Order) -> Optional[str]:
        if o.side == "BUY" and o.price > self.limits.max_buy_price:
            return f"price {o.price} > max_buy_price {self.limits.max_buy_price}"
        stake = o.price * o.size
        if stake > self.limits.max_stake_per_market:
            return f"stake ${stake:.2f} > per-market cap ${self.limits.max_stake_per_market}"
        if self.exposure + stake > self.limits.max_total_exposure:
            return f"exposure cap ${self.limits.max_total_exposure} exceeded"
        return None

    def submit(self, o: Order) -> dict:
        reason = self._check(o)
        if reason:
            rec = {"ts": time.time(), "status": "REJECTED", "reason": reason, **asdict(o)}
            self._log(rec)
            return rec
        # simulate a taker fill against the current book
        fill = None
        try:
            bk = self.pm.book(o.token_id)
            fill = bk.cost_to_buy(o.size) if o.side == "BUY" else bk.proceeds_to_sell(o.size)
        except Exception as e:
            rec = {"ts": time.time(), "status": "ERROR", "reason": str(e), **asdict(o)}
            self._log(rec)
            return rec
        if fill is None:
            rec = {"ts": time.time(), "status": "NO_FILL", "reason": "insufficient depth", **asdict(o)}
            self._log(rec)
            return rec
        self.exposure += fill * o.size if o.side == "BUY" else -fill * o.size
        rec = {"ts": time.time(), "status": "FILLED_PAPER", "fill_price": round(fill, 4),
               "stake": round(fill * o.size, 2), **asdict(o)}
        self._log(rec)
        return rec

    def _log(self, rec: dict) -> None:
        with open(self.log_path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # ---- resting-order interface (maker reconcile; file-backed so it survives reruns) ----
    @property
    def _oo_path(self) -> Path:
        return DATA / "paper_open_orders.json"

    def _load_oo(self) -> dict:
        return json.loads(self._oo_path.read_text()) if self._oo_path.exists() else {}

    def _save_oo(self, d: dict) -> None:
        self._oo_path.write_text(json.dumps(d, indent=2))

    def place(self, o: Order, post_only: bool = True) -> dict:
        reason = self._check(o)
        if reason:
            return {"status": "REJECTED", "reason": reason}
        import uuid
        oid = "paper-" + uuid.uuid4().hex[:10]
        oo = self._load_oo()
        oo[oid] = {"id": oid, "token_id": o.token_id, "side": o.side, "price": o.price,
                   "size": o.size, "city": o.city, "bucket": o.bucket, "ts": time.time()}
        self._save_oo(oo)
        return {"status": "RESTING", "id": oid, "price": o.price, "size": o.size}

    def open_orders(self, market: Optional[str] = None) -> list:
        return list(self._load_oo().values())

    def cancel(self, oid: str) -> dict:
        oo = self._load_oo()
        if oid in oo:
            oo.pop(oid)
            self._save_oo(oo)
            return {"status": "CANCELLED", "id": oid}
        return {"status": "NOT_FOUND", "id": oid}

    def cancel_all(self) -> dict:
        n = len(self._load_oo())
        self._save_oo({})
        return {"status": "CANCELLED_ALL", "n": n}


class LiveBroker:
    """Real orders — disabled unless explicitly enabled by env. Provided for
    completeness; YOU run it with YOUR keys. It places LIMIT orders only."""

    def __init__(self, limits: Optional[RiskLimits] = None):
        if os.getenv("WXBOT_LIVE") != "1":
            raise RuntimeError(
                "LiveBroker disabled. Set WXBOT_LIVE=1 and provide POLY_PK + POLY_FUNDER "
                "to trade real money. Validate on PaperBroker first.")
        try:
            from py_clob_client.client import ClobClient  # noqa: F401
        except ImportError as e:
            raise RuntimeError("pip install py-clob-client to use LiveBroker") from e
        pk = os.getenv("POLY_PK")
        funder = os.getenv("POLY_FUNDER")
        if not pk or not funder:
            raise RuntimeError("Set POLY_PK (private key) and POLY_FUNDER (proxy wallet).")
        from py_clob_client.client import ClobClient
        self.limits = limits or RiskLimits()
        self.client = ClobClient(
            "https://clob.polymarket.com", key=pk, chain_id=137,
            signature_type=2, funder=funder)
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def place(self, o: Order, post_only: bool = True) -> dict:
        """Rest a limit order. post_only=True => maker-only, never crosses (no taker fill)."""
        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY, SELL
        if o.side == "BUY" and o.price > self.limits.max_buy_price:
            return {"status": "REJECTED", "reason": "max_buy_price"}
        args = OrderArgs(price=o.price, size=o.size, side=BUY if o.side == "BUY" else SELL,
                         token_id=o.token_id)
        signed = self.client.create_order(args)
        return self.client.post_order(signed, post_only=post_only)

    # back-compat: submit == place (maker by default)
    submit = place

    def open_orders(self, market: Optional[str] = None) -> list:
        from py_clob_client.clob_types import OpenOrderParams
        res = self.client.get_orders(OpenOrderParams(market=market) if market else None)
        out = []
        for o in (res or []):
            out.append({"id": o.get("id"), "token_id": o.get("asset_id"),
                        "side": o.get("side"), "price": float(o.get("price", 0) or 0),
                        "size": float(o.get("original_size", 0) or 0),
                        "city": "", "bucket": ""})
        return out

    def cancel(self, order_id: str) -> dict:
        return self.client.cancel(order_id)

    def cancel_all(self) -> dict:
        return self.client.cancel_all()
