"""Station observations — the data the market ACTUALLY resolves on.

Open-Meteo's grid only lands the resolved bucket ~48% of the time; the airport
station's own METAR lands it ~96%. So this client, keyed to the resolution ICAO,
is the heart of a viable strategy.

  historical (backtest)  : Iowa State IEM ASOS archive  (free, global, by ICAO)
  realtime  (live)       : aviationweather.gov METAR API (free, global)

We always read in the market's unit and filter to the LOCAL calendar day, since
the resolution is "the day's high at that station in local time".
"""
from __future__ import annotations
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import HEADERS
from .polymarket import make_session

IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
AWC = "https://aviationweather.gov/api/data/metar"
_CACHE = Path(__file__).resolve().parent.parent.parent / "data" / "metarcache"


def iem_station_id(icao: str) -> str:
    """IEM drops the leading K for CONUS stations; international keeps the ICAO."""
    return icao[1:] if (len(icao) == 4 and icao.startswith("K")) else icao


def _c_to_unit(c: float, unit: str) -> float:
    return c * 9 / 5 + 32 if unit.upper() == "F" else c


class Metar:
    def __init__(self, timeout: float = 45.0, cache: bool = True):
        self.s = make_session(HEADERS)
        self.timeout = timeout
        self.cache = cache
        if cache:
            _CACHE.mkdir(parents=True, exist_ok=True)

    # ---------- historical (IEM) ----------
    def _iem_rows(self, icao: str, tz: str, day: date) -> List[Tuple[datetime, float]]:
        """All (local_dt, temp_c) obs spanning `day` in the station's local tz."""
        sid = iem_station_id(icao)
        key = hashlib.md5(f"{sid}|{tz}|{day}".encode()).hexdigest()
        fp = _CACHE / f"{key}.json"
        if self.cache and fp.exists():
            raw = json.loads(fp.read_text())
        else:
            # pull a 2-day UTC window so local-day edges are covered, tz-localized by IEM
            d2 = day.toordinal() + 2
            from datetime import date as _date
            end = _date.fromordinal(d2)
            params = {
                "station": sid, "data": "tmpc", "tz": tz,
                "year1": day.year, "month1": day.month, "day1": day.day,
                "year2": end.year, "month2": end.month, "day2": end.day,
                "format": "onlycomma", "missing": "empty", "latlon": "no",
            }
            r = self.s.get(IEM, params=params, timeout=self.timeout)
            r.raise_for_status()
            raw = r.text
            if self.cache:
                fp.write_text(json.dumps(raw))
        rows = []
        for line in raw.splitlines()[1:]:
            p = line.split(",")
            if len(p) >= 3 and p[2] not in ("", "M"):
                try:
                    dt = datetime.strptime(p[1].strip(), "%Y-%m-%d %H:%M")
                    rows.append((dt, float(p[2])))
                except ValueError:
                    continue
        return rows

    def daily_max(self, icao: str, tz: str, unit: str, day: date) -> Optional[float]:
        vals = [c for (dt, c) in self._iem_rows(icao, tz, day) if dt.date() == day]
        return _c_to_unit(max(vals), unit) if vals else None

    def high_so_far(self, icao: str, tz: str, unit: str, day: date,
                    until_hour_local: int) -> Optional[float]:
        vals = [c for (dt, c) in self._iem_rows(icao, tz, day)
                if dt.date() == day and dt.hour <= until_hour_local]
        return _c_to_unit(max(vals), unit) if vals else None

    def low_so_far(self, icao: str, tz: str, unit: str, day: date,
                   until_hour_local: int) -> Optional[float]:
        """Lowest temp observed up to `until_hour_local` — mirror of high_so_far
        for 'Lowest temperature in X' markets."""
        vals = [c for (dt, c) in self._iem_rows(icao, tz, day)
                if dt.date() == day and dt.hour <= until_hour_local]
        return _c_to_unit(min(vals), unit) if vals else None

    # ---------- realtime (aviationweather.gov) ----------
    def realtime_high_so_far(self, icao: str, tz: str, unit: str,
                             day: Optional[date] = None, hours: int = 18) -> Optional[float]:
        try:
            from zoneinfo import ZoneInfo
            now_local = datetime.now(ZoneInfo(tz))
        except Exception:
            now_local = datetime.utcnow()
        day = day or now_local.date()
        r = self.s.get(AWC, params={"ids": icao, "format": "json", "hours": hours},
                       timeout=self.timeout)
        r.raise_for_status()
        obs = r.json()
        temps = []
        for o in obs:
            t = o.get("temp")
            rt = o.get("reportTime") or o.get("obsTime")
            if t is None:
                continue
            try:
                from zoneinfo import ZoneInfo
                ts = datetime.fromisoformat(str(rt).replace("Z", "+00:00")).astimezone(ZoneInfo(tz))
                if ts.date() == day:
                    temps.append(float(t))
            except Exception:
                temps.append(float(t))
        return _c_to_unit(max(temps), unit) if temps else None
