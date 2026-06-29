#!/usr/bin/env python
"""Maker runner — reconcile reward-qualifying NO bids / stale-discount sweeps,
confined to verified cities and risk caps.

  python scripts/run_maker.py                      # PAPER reconcile once (no money)
  python scripts/run_maker.py --loop 300           # PAPER reconcile every 300s
  python scripts/run_maker.py --place              # place fresh (no cancel/reconcile)
  python scripts/run_maker.py --cancel-all         # kill switch: cancel all resting

  WXBOT_LIVE=1 POLY_PK=.. POLY_FUNDER=.. \
      python scripts/run_maker.py --live --loop 300   # REAL maker orders, auto-reconcile

  options: --stake 50  --max-orders 6  --cap 250

Reconcile = cancel orders no longer wanted (resolved / not frontier / price drifted)
and place the missing ones. It skips all cancels if the market scan returns no events
(transient-failure guard). Live mode needs WXBOT_LIVE=1 + both keys + py-clob-client.
"""
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxbot.maker import run, reconcile, cancel_all


def _arg(flag, default, cast=float):
    return cast(sys.argv[sys.argv.index(flag) + 1]) if flag in sys.argv else default


if __name__ == "__main__":
    live = "--live" in sys.argv
    kw = dict(live=live, stake_per=_arg("--stake", 50.0),
              max_orders=int(_arg("--max-orders", 6, int)), total_cap=_arg("--cap", 250.0))

    if "--cancel-all" in sys.argv:
        cancel_all(live=live)
        sys.exit(0)

    if live:
        print("⚠️  LIVE mode: places/cancels REAL orders with your funded wallet.\n")

    if "--loop" in sys.argv:
        interval = _arg("--loop", 300.0)
        print(f"reconcile loop every {interval:.0f}s — Ctrl-C to stop "
              "(orders stay resting; run --cancel-all to clear).")
        try:
            while True:
                (run if "--place" in sys.argv else reconcile)(**kw)
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped looping (resting orders left in place).")
    else:
        (run if "--place" in sys.argv else reconcile)(**kw)
