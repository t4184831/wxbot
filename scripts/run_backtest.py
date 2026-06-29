#!/usr/bin/env python
"""Validate the strategy on resolved markets, no lookahead.

  python scripts/run_backtest.py [events_limit] [hours_csv] [source]
  source = metar (default, station obs) | grid (Open-Meteo, ~48% ceiling)
  e.g. python scripts/run_backtest.py 80 17 metar
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxbot.backtest import run, print_summary

if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    hours = [int(h) for h in sys.argv[2].split(",")] if len(sys.argv) > 2 else [17]
    source = sys.argv[3] if len(sys.argv) > 3 else "metar"
    s = run(events_limit=limit, hours=hours, source=source, verbose=True)
    print_summary(s)
