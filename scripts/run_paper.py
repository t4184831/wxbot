#!/usr/bin/env python
"""Gate A runner: scan live afternoon windows, record signals as paper positions,
settle any matured ones against real resolution, print the running track record.

Designed to be run on a schedule (e.g. every 30 min) so it accumulates an honest
paper record across each city's local afternoon.

  python scripts/run_paper.py [stake]
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxbot.scanner import scan
from wxbot.paper import record, settle, report

if __name__ == "__main__":
    stake = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    print(f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] paper run")

    signals = scan(anchored_only=True, paper=False, verbose=True)
    added = record(signals, stake=stake, anchored_only=True)
    print(f"recorded {added} new paper position(s)")

    n = settle(verbose=True)
    print(f"settled {n} matured position(s)")

    report()
