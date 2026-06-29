"""Bucket-probability model.

Inputs : ensemble member forecasts of the day's MAX temp (+ optional observed
         high-so-far floor for intraday) and the event's buckets.
Output : a calibrated probability per bucket.

Two ideas do all the work:
  1. The realized high is an INTEGER degree (resolution rounds to whole degrees),
     so we round simulated highs before bucketing.
  2. Intraday, the observed high-so-far is a hard FLOOR: the final high can only
     be >= it. As the afternoon progresses this floor rises and the distribution
     collapses onto one bucket -> that collapse is the trader's main edge.

A sigma floor (station-vs-grid bias + rounding) is always added so we never claim
100% — overconfidence is what turns a +5c grind into a -90c blow-up.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from .parse import Bucket
from .config import MODEL


@dataclass
class ModelResult:
    probs: Dict[str, float]            # bucket label -> probability
    favorite: str                     # label of argmax bucket
    fav_prob: float
    mean_high: float
    std_high: float
    floor: Optional[float]            # observed high-so-far used, if any
    n_members: int
    intraday: bool

    def prob_of(self, label: str) -> float:
        return self.probs.get(label, 0.0)


def bucket_probs(samples: List[float], buckets: List[Bucket],
                 floor: Optional[float] = None,
                 sigma: Optional[float] = None,
                 unit: str = "F", seed: int = 0,
                 draws_per_member: int = 300) -> Optional[ModelResult]:
    """Monte-Carlo the realized integer-degree high and integrate over buckets."""
    samples = [s for s in samples if s is not None and np.isfinite(s)]
    if not samples:
        return None
    rng = np.random.default_rng(seed)
    arr = np.asarray(samples, dtype=float)

    # sigma floor in market unit (config is in C; ~1.8x for F)
    sig = sigma if sigma is not None else MODEL.sigma_floor_c * (1.8 if unit.upper() == "F" else 1.0)

    if floor is not None:
        arr = np.maximum(arr, floor)          # final high >= observed high-so-far
    # convolve each member with grid/station noise, then round to integer degrees
    pool = arr[:, None] + rng.normal(0.0, sig, size=(len(arr), draws_per_member))
    ints = np.rint(pool.ravel())

    counts = {}
    n = ints.size
    for b in buckets:
        mask = (ints >= b.lo) & (ints <= b.hi)
        counts[b.label] = float(mask.sum())
    total = sum(counts.values())
    if total <= 0:
        return None
    probs = {k: v / total for k, v in counts.items()}
    fav = max(probs, key=probs.get)
    return ModelResult(
        probs=probs, favorite=fav, fav_prob=probs[fav],
        mean_high=float(arr.mean()), std_high=float(arr.std()),
        floor=floor, n_members=len(samples), intraday=floor is not None,
    )


def price_event(weather, ev, decision_local_hour: Optional[int] = None,
                seed: int = 0) -> Optional[ModelResult]:
    """Grid-only pricing (the ~48% regime). Kept for comparison/fallback."""
    if ev.day is None:
        return None
    samples = weather.ensemble_daily_max(ev.lat, ev.lon, ev.tz, ev.unit, ev.day)
    if not samples:
        fc = weather.forecast_daily_max(ev.lat, ev.lon, ev.tz, ev.unit, ev.day)
        samples = [fc] if fc is not None else []
    floor = None
    if decision_local_hour is not None:
        floor = weather.observed_high_so_far(ev.lat, ev.lon, ev.tz, ev.unit, ev.day)
    return bucket_probs(samples, ev.buckets, floor=floor, unit=ev.unit, seed=seed)


# station obs (METAR) is the actual resolution source -> tight residual uncertainty
METAR_SIGMA_C = 0.5


def price_event_live(weather, metar, ev, biases: Optional[Dict[str, float]] = None,
                     seed: int = 0):
    """Production pricing. Anchors on the resolution station's live METAR high-so-far
    (a HARD lower bound — the high can only rise from here) and uses the bias-corrected
    ensemble for the remaining-day upside.

    - Late in the local day the high is in: ensemble members fall below the floor, the
      whole distribution pins to the station obs -> tight, high-confidence bucket.
    - Earlier, ensemble members above the floor keep the distribution wide -> low
      confidence -> the scanner's thresholds suppress the (validated-to-lose) early bets.

    Returns (ModelResult, anchored: bool) where anchored means a METAR floor was used.
    """
    if ev.day is None:
        return None, False
    biases = biases or {}
    bias = biases.get(ev.icao or ev.city, 0.0)

    # local hour at the station, and whether the day's high is essentially in
    try:
        from zoneinfo import ZoneInfo
        local_hour = datetime.now(ZoneInfo(ev.tz)).hour
        on_day = datetime.now(ZoneInfo(ev.tz)).date() == ev.day
    except Exception:
        local_hour, on_day = 12, True

    floor = None
    if ev.icao:
        try:
            floor = metar.realtime_high_so_far(ev.icao, ev.tz, ev.unit, ev.day)
        except Exception:
            floor = None

    tight = METAR_SIGMA_C * (1.8 if ev.unit.upper() == "F" else 1.0)
    # VALIDATED regime: it's the event day, past mid-afternoon, high is observable ->
    # pin to the station obs alone (this is the 74-97% backtest model). Shrink the
    # band as the day closes: at peak the high may still climb (~0.5C), but hours
    # later it is locked (~0.2C), so confidence should rise toward the ~97% argmax
    # accuracy the backtest showed.
    if floor is not None and on_day and local_hour >= MODEL.peak_hour_local:
        hours_past = local_hour - MODEL.peak_hour_local
        sig_c = max(0.2, METAR_SIGMA_C - 0.1 * hours_past)
        sig = sig_c * (1.8 if ev.unit.upper() == "F" else 1.0)
        mr = bucket_probs([floor], ev.buckets, floor=floor, sigma=sig,
                          unit=ev.unit, seed=seed)
        return mr, True

    # Morning / pre-peak: use the bias-corrected ensemble forecast, with the floor
    # (if any) as a hard lower bound. Lower confidence by design -> scanner suppresses.
    samples = weather.ensemble_daily_max(ev.lat, ev.lon, ev.tz, ev.unit, ev.day)
    if not samples:
        fc = weather.forecast_daily_max(ev.lat, ev.lon, ev.tz, ev.unit, ev.day)
        samples = [fc] if fc is not None else []
    samples = [s + bias for s in samples]
    if not samples:
        if floor is None:
            return None, False
        samples = [floor]
    mr = bucket_probs(samples, ev.buckets, floor=floor,
                      sigma=tight if floor is not None else None,
                      unit=ev.unit, seed=seed)
    return mr, floor is not None
