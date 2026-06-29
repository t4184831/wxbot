"""Weather client over Open-Meteo (free, no key, global).

Three things the model needs:
  1. ensemble spread of the day's MAX temp  -> bucket uncertainty (forecast mode)
  2. today's observed hourly temps so far    -> a hard floor on the high (intraday)
  3. historical hourly temps + actual max     -> backtesting

We always request in the market's own unit (F/C) so there is no conversion bug
between what we price and what the market resolves on.
"""
from __future__ import annotations
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import OM_FORECAST, OM_ENSEMBLE, OM_ARCHIVE, HEADERS
from .polymarket import make_session

_CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "wxcache"

# ensemble systems to blend; each contributes ~20-50 members
ENSEMBLE_MODELS = "gfs_seamless,icon_seamless,ecmwf_ifs025"


def _unit_param(unit: str) -> str:
    return "fahrenheit" if unit.upper() == "F" else "celsius"


class Weather:
    def __init__(self, timeout: float = 30.0, cache: bool = True):
        self.s = make_session(HEADERS)
        self.timeout = timeout
        self.cache = cache
        if cache:
            _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _get(self, url: str, params: dict) -> dict:
        # cache historical (archive) responses to disk — they're immutable, so
        # backtests re-run instantly after the first pass.
        use_cache = self.cache and "archive" in url
        if use_cache:
            key = hashlib.md5((url + json.dumps(params, sort_keys=True)).encode()).hexdigest()
            fp = _CACHE_DIR / f"{key}.json"
            if fp.exists():
                return json.loads(fp.read_text())
        r = self.s.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        js = r.json()
        if use_cache:
            fp.write_text(json.dumps(js))
        return js

    # ---------- forecast (live / today) ----------
    def ensemble_daily_max(self, lat: float, lon: float, tz: str, unit: str,
                           day: date) -> List[float]:
        """Per-member forecast of the day's MAX temp (market unit). The empirical
        distribution of these members is our bucket-probability source."""
        js = self._get(OM_ENSEMBLE, {
            "latitude": lat, "longitude": lon, "timezone": tz,
            "temperature_unit": _unit_param(unit), "models": ENSEMBLE_MODELS,
            "hourly": "temperature_2m",
            "start_date": day.isoformat(), "end_date": day.isoformat(),
        })
        hourly = js.get("hourly", {})
        maxes: List[float] = []
        for key, series in hourly.items():
            if not key.startswith("temperature_2m"):
                continue
            vals = [v for v in series if v is not None]
            if vals:
                maxes.append(max(vals))
        return maxes

    def forecast_daily_max(self, lat: float, lon: float, tz: str, unit: str,
                           day: date) -> Optional[float]:
        js = self._get(OM_FORECAST, {
            "latitude": lat, "longitude": lon, "timezone": tz,
            "temperature_unit": _unit_param(unit),
            "daily": "temperature_2m_max", "forecast_days": 7,
        })
        d = js.get("daily", {})
        times, vals = d.get("time", []), d.get("temperature_2m_max", [])
        for t, v in zip(times, vals):
            if t == day.isoformat() and v is not None:
                return v
        return None

    def observed_high_so_far(self, lat: float, lon: float, tz: str, unit: str,
                             day: date) -> Optional[float]:
        """Max observed temp for `day` up to *now in the event's timezone* (uses the
        forecast API's past hours, which are returned in local time). Returns None
        if the day hasn't started locally."""
        try:
            from zoneinfo import ZoneInfo
            now_local = datetime.now(ZoneInfo(tz)).replace(tzinfo=None)
        except Exception:
            now_local = datetime.utcnow()
        hrs = self._forecast_hourly(lat, lon, tz, unit, past_days=2, forecast_days=1)
        todays = [v for (t, v) in hrs if t.date() == day and t <= now_local]
        vals = [v for v in todays if v is not None]
        return max(vals) if vals else None

    def _forecast_hourly(self, lat, lon, tz, unit, past_days=2, forecast_days=2
                         ) -> List[Tuple[datetime, Optional[float]]]:
        js = self._get(OM_FORECAST, {
            "latitude": lat, "longitude": lon, "timezone": tz,
            "temperature_unit": _unit_param(unit), "hourly": "temperature_2m",
            "past_days": past_days, "forecast_days": forecast_days,
        })
        h = js.get("hourly", {})
        out = []
        for t, v in zip(h.get("time", []), h.get("temperature_2m", [])):
            out.append((datetime.fromisoformat(t), v))
        return out

    # ---------- archive (backtest) ----------
    def archive_hourly(self, lat: float, lon: float, tz: str, unit: str,
                       day: date) -> List[Tuple[datetime, Optional[float]]]:
        js = self._get(OM_ARCHIVE, {
            "latitude": lat, "longitude": lon, "timezone": tz,
            "temperature_unit": _unit_param(unit), "hourly": "temperature_2m",
            "start_date": day.isoformat(), "end_date": day.isoformat(),
        })
        h = js.get("hourly", {})
        return [(datetime.fromisoformat(t), v)
                for t, v in zip(h.get("time", []), h.get("temperature_2m", []))]

    def archive_daily_max(self, lat: float, lon: float, tz: str, unit: str,
                          day: date) -> Optional[float]:
        """The actual realized daily max (our backtest ground truth proxy)."""
        rows = self.archive_hourly(lat, lon, tz, unit, day)
        vals = [v for (_, v) in rows if v is not None]
        return max(vals) if vals else None

    def archive_high_so_far(self, lat: float, lon: float, tz: str, unit: str,
                            day: date, until_hour_local: int) -> Optional[float]:
        """Backtest analogue of observed_high_so_far: max up to `until_hour_local`."""
        rows = self.archive_hourly(lat, lon, tz, unit, day)
        vals = [v for (t, v) in rows if v is not None and t.hour <= until_hour_local]
        return max(vals) if vals else None
