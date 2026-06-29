"""Per-station calibration — the piece that makes grid weather usable.

Open-Meteo's grid reads each station with a CONSISTENT offset (Milan +2C hot,
Munich -2C cold). We learn that offset per station from resolved history and
subtract it. Crucially we evaluate LEAVE-ONE-OUT so the reported accuracy is
honest (a station's own bias is estimated without the event being scored).

This answers the architecture question: after correction, how accurately can
grid data pin the resolved bucket? That ceiling decides whether we can trade on
grid data alone or must ingest the actual resolution-station obs (METAR).
"""
from __future__ import annotations
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .clients import Polymarket, Weather
from .parse import build_event
from .recon import TARGET

DATA = Path(__file__).resolve().parent.parent / "data"
DATASET = DATA / "calib_dataset.json"


def _center(lo: float, hi: float) -> float:
    inf = float("inf")
    if lo == -inf:
        return hi
    if hi == inf:
        return lo
    return (lo + hi) / 2


def build_dataset(trader: str = TARGET, max_events: int = 120,
                  hours: Optional[List[int]] = None, verbose: bool = True) -> List[dict]:
    """Pull resolved events the trader played; record OM max + obs-so-far + the
    resolved bucket. Cached to data/calib_dataset.json."""
    hours = hours or [15, 16, 17, 18]
    pm, wx = Polymarket(), Weather()
    acts = pm.activity(trader, limit=1000)
    slugs: List[str] = []
    for a in acts:
        s = a.get("eventSlug")
        if s and s not in slugs:
            slugs.append(s)

    rows: List[dict] = []
    for slug in slugs[:max_events]:
        e = pm.event_by_slug(slug)
        if not e:
            continue
        ev = build_event(e)
        if not ev or not ev.day or not ev.closed or len(ev.buckets) < 3:
            continue
        win = max(ev.buckets, key=lambda b: b.price_yes)
        if win.price_yes < 0.5:
            continue
        try:
            om_max = wx.archive_daily_max(ev.lat, ev.lon, ev.tz, ev.unit, ev.day)
            obs = {str(h): wx.archive_high_so_far(ev.lat, ev.lon, ev.tz, ev.unit, ev.day, h)
                   for h in hours}
        except Exception:
            continue
        if om_max is None:
            continue
        rows.append({
            "station": ev.icao or ev.city, "city": ev.city, "unit": ev.unit,
            "day": ev.day.isoformat(), "lat": ev.lat, "lon": ev.lon,
            "om_max": om_max, "obs": obs,
            "win_lo": win.lo, "win_hi": win.hi, "win_label": win.label,
            "center": _center(win.lo, win.hi),
            "buckets": [[b.lo, b.hi, b.label] for b in ev.buckets],
        })
        if verbose and len(rows) % 10 == 0:
            print(f"  collected {len(rows)} events...")
    DATA.mkdir(exist_ok=True)
    DATASET.write_text(json.dumps(rows))
    if verbose:
        print(f"dataset: {len(rows)} resolved events -> {DATASET}")
    return rows


def load_dataset() -> List[dict]:
    return json.loads(DATASET.read_text()) if DATASET.exists() else []


def station_bias(rows: List[dict], using: str = "om_max") -> Dict[str, float]:
    """Median (resolved_center - estimate) per station, in the market unit."""
    acc: Dict[str, list] = defaultdict(list)
    for r in rows:
        est = _est(r, using)
        if est is not None:
            acc[r["station"]].append(r["center"] - est)
    return {k: statistics.median(v) for k, v in acc.items() if v}


def _est(r: dict, using: str) -> Optional[float]:
    if using == "om_max":
        return r["om_max"]
    return r["obs"].get(using)   # e.g. "16" -> obs-so-far at 16:00


def _in_bucket(est: float, r: dict) -> bool:
    return r["win_lo"] <= round(est) <= r["win_hi"]


def evaluate(rows: Optional[List[dict]] = None, using: str = "om_max") -> Dict:
    """Leave-one-out: correct each event with its station's bias computed from the
    OTHER events, then check if the corrected estimate lands in the resolved bucket."""
    rows = rows or load_dataset()
    rows = [r for r in rows if _est(r, using) is not None]
    if not rows:
        return {"n": 0}
    by_station: Dict[str, list] = defaultdict(list)
    for r in rows:
        by_station[r["station"]].append(r)

    raw_hits = sum(_in_bucket(_est(r, using), r) for r in rows)
    loo_hits = 0
    per_station: Dict[str, dict] = {}
    for st, srows in by_station.items():
        diffs = [r["center"] - _est(r, using) for r in srows]
        st_hits = 0
        for i, r in enumerate(srows):
            others = diffs[:i] + diffs[i + 1:]
            bias = statistics.median(others) if others else 0.0
            if _in_bucket(_est(r, using) + bias, r):
                loo_hits += 1
                st_hits += 1
        per_station[st] = {
            "city": srows[0]["city"], "unit": srows[0]["unit"], "n": len(srows),
            "bias": round(statistics.median(diffs), 2),
            "resid_std": round(statistics.pstdev(diffs), 2) if len(diffs) > 1 else 0.0,
            "loo_hit_rate": round(st_hits / len(srows), 3),
        }
    n = len(rows)
    return {
        "n": n, "using": using,
        "raw_hit_rate": round(raw_hits / n, 3),
        "loo_corrected_hit_rate": round(loo_hits / n, 3),
        "per_station": dict(sorted(per_station.items(), key=lambda x: -x[1]["n"])),
    }


def print_eval(ev: Dict) -> None:
    if not ev.get("n"):
        print("no data — run build_dataset() first")
        return
    print(f"\n=== CALIBRATION ({ev['using']}, n={ev['n']}) ===")
    print(f"raw in-bucket          : {ev['raw_hit_rate']*100:.0f}%")
    print(f"bias-corrected (LOO)   : {ev['loo_corrected_hit_rate']*100:.0f}%   <- honest ceiling")
    print(f"\n{'station':<8}{'city':<13}{'u':<2}{'n':>3}{'bias':>7}{'residSD':>8}{'LOOhit':>8}")
    for st, d in ev["per_station"].items():
        print(f"{st:<8}{d['city']:<13}{d['unit']:<2}{d['n']:>3}{d['bias']:>+7.2f}"
              f"{d['resid_std']:>8.2f}{d['loo_hit_rate']*100:>7.0f}%")
