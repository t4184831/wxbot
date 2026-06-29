#!/usr/bin/env python
"""Scan open temperature markets for live edge (METAR-anchored).

  python scripts/run_scan.py [min_edge] [--paper] [--all] [--preferred]
    --paper      route signals to the PaperBroker (no real money)
    --all        include grid-only signals (default: METAR-anchored only)
    --preferred  restrict to stable preferred cities
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxbot.scanner import scan

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    scan(
        min_edge=float(args[0]) if args else None,
        preferred_only="--preferred" in sys.argv,
        anchored_only="--all" not in sys.argv,
        paper="--paper" in sys.argv,
    )
