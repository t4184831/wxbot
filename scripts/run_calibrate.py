#!/usr/bin/env python
"""Build the calibration dataset and report bias-corrected accuracy.

  python scripts/run_calibrate.py [max_events] [--rebuild]
Evaluates full-day OM max and each obs-so-far decision hour.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxbot.calibrate import build_dataset, load_dataset, evaluate, print_eval

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rebuild = "--rebuild" in sys.argv
    max_events = int(args[0]) if args else 120
    rows = load_dataset()
    if rebuild or not rows:
        rows = build_dataset(max_events=max_events)
    for using in ["om_max", "18", "17", "16", "15"]:
        print_eval(evaluate(rows, using=using))
