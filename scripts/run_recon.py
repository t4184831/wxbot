#!/usr/bin/env python
"""Recon the target trader (or any wallet): python scripts/run_recon.py [0xADDR]"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxbot.recon import analyze, TARGET

if __name__ == "__main__":
    analyze(sys.argv[1] if len(sys.argv) > 1 else TARGET)
