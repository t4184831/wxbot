"""Parse Polymarket temperature events into something the model can price.

An event ("Highest temperature in Austin on June 12?") contains N binary
sub-markets, one per temperature bucket ("92-93F", "81F or below",
"100F or higher"). We need, per event:
  - the resolution station (ICAO) + lat/lon/tz
  - the unit (F or C)
  - each bucket as a numeric half-open-ish integer interval [lo, hi] (inclusive)
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date
from typing import List, Optional, Tuple

from .config import STATIONS, OM_GEOCODE, HEADERS
import requests

INF = float("inf")


@dataclass
class Bucket:
    token_id: str          # CLOB token id for the YES outcome of this bucket
    label: str             # "92-93F"
    lo: float              # inclusive lower bound in market unit (-inf for "X or below")
    hi: float              # inclusive upper bound in market unit (+inf for "X or higher")
    price_yes: float       # last gamma snapshot price of YES
    market_id: str
    no_token_id: str = ""  # CLOB token id for the NO outcome (the trader's scalp side)

    def contains(self, temp_int: float) -> bool:
        return self.lo <= temp_int <= self.hi

    @property
    def width(self) -> float:
        if self.lo == -INF or self.hi == INF:
            return INF
        return self.hi - self.lo + 1  # integer degrees covered


@dataclass
class TempEvent:
    event_id: str
    city: str
    day: Optional[date]
    unit: str              # "F" or "C"
    icao: Optional[str]
    lat: float
    lon: float
    tz: str
    buckets: List[Bucket]
    closed: bool
    kind: str = "high"     # "high" (Highest temp) | "low" (Lowest temp)

    @property
    def total_yes_price(self) -> float:
        return sum(b.price_yes for b in self.buckets)


def parse_unit(text: str) -> str:
    t = text.lower()
    # buckets carry the unit; default to F for US-style "92-93" if ambiguous
    if "°c" in t or " c " in t or "celsius" in t or re.search(r"\d+\s*c\b", t):
        if "°f" in t or "fahrenheit" in t:
            # mixed — trust the bucket labels later; tie-break to F
            return "F"
        return "C"
    return "F"


def parse_bucket_label(label: str) -> Optional[Tuple[float, float]]:
    """'92-93°F' -> (92,93); '81°F or below' -> (-inf,81); '100°F or higher' -> (100,inf);
    '13°C' -> (13,13). Returns None if unparseable.

    NB: the '-' in '92-93' is a RANGE separator, not a minus sign — parse the range
    explicitly so we don't read it as [92, -93]."""
    s = label.replace("°", "").replace("º", "").lower().strip()
    low = "below" in s or "or less" in s or "under" in s
    high = "higher" in s or "above" in s or "or more" in s or "over" in s
    # range: two numbers joined by a dash / 'to' (allow leading minus on the first only)
    rng = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)", s)
    single = re.search(r"-?\d+(?:\.\d+)?", s)
    if low and single:
        return (-INF, float(single.group()))
    if high and single:
        return (float(single.group()), INF)
    if rng:
        a, b = float(rng.group(1)), float(rng.group(2))
        return (min(a, b), max(a, b))
    if single:
        v = float(single.group())
        return (v, v)
    return None


def extract_icao(description: str) -> Optional[str]:
    """Pull the ICAO station code from the Wunderground resolution URL in the desc."""
    if not description:
        return None
    # .../history/daily/us/tx/austin/KAUS  or .../KAUS.html
    m = re.search(r"/([A-Z]{4})(?:\.html|/|\b)", description)
    if m and m.group(1) in STATIONS:
        return m.group(1)
    # any 4-letter upper token that looks like an ICAO near 'Station'
    for cand in re.findall(r"\b([A-Z]{4})\b", description):
        if cand in STATIONS:
            return cand
    return m.group(1) if m else None


_MONTHS = {m: i for i, m in enumerate(
    ["january","february","march","april","may","june","july","august",
     "september","october","november","december"], 1)}


def parse_day(title: str, slug: str, year_hint: int) -> Optional[date]:
    text = f"{title} {slug}".lower()
    m = re.search(r"(january|february|march|april|may|june|july|august|september|october|november|december)[ -](\d{1,2})", text)
    if not m:
        return None
    mo = _MONTHS[m.group(1)]
    dy = int(m.group(2))
    ym = re.search(r"(20\d{2})", text)
    yr = int(ym.group(1)) if ym else year_hint
    try:
        return date(yr, mo, dy)
    except ValueError:
        return None


def parse_city(title: str) -> str:
    m = re.search(r"temperature in ([A-Za-z .'-]+?)(?:\s+on\b|\s+be\b|\?)", title)
    return m.group(1).strip() if m else title


_geo_cache: dict = {}


def geocode(city: str) -> Optional[Tuple[float, float, str]]:
    if city in _geo_cache:
        return _geo_cache[city]
    try:
        r = requests.get(OM_GEOCODE, params={"name": city, "count": 1},
                         headers=HEADERS, timeout=15)
        r.raise_for_status()
        res = r.json().get("results") or []
        if res:
            g = res[0]
            out = (g["latitude"], g["longitude"], g.get("timezone", "UTC"))
            _geo_cache[city] = out
            return out
    except requests.RequestException:
        pass
    return None


# When True, any ICAO that Polymarket names (even if not pre-listed in STATIONS)
# is resolved to lat/lon/tz on the fly -> METAR coverage for EVERY city.
AUTO_RESOLVE = False
_STATION_CACHE_PATH = __file__.rsplit("/", 2)[0] + "/data/station_cache.json"
_station_cache = None


def resolve_station(icao):
    """ICAO -> (lat, lon, tz), resolved dynamically (aviationweather station info
    + Open-Meteo timezone) and cached to disk. Returns None on failure."""
    global _station_cache
    if _station_cache is None:
        try:
            import json as _j
            _station_cache = _j.load(open(_STATION_CACHE_PATH))
        except Exception:
            _station_cache = {}
    if icao in _station_cache:
        v = _station_cache[icao]
        return tuple(v) if v else None
    out = None
    try:
        r = requests.get(f"https://aviationweather.gov/api/data/stationinfo?ids={icao}&format=json",
                         headers=HEADERS, timeout=20, verify=False).json()
        if r:
            lat, lon = float(r[0]["lat"]), float(r[0]["lon"])
            m = requests.get("https://api.open-meteo.com/v1/forecast",
                             params={"latitude": lat, "longitude": lon,
                                     "timezone": "auto", "forecast_days": 1},
                             timeout=20, verify=False).json()
            tz = m.get("timezone")
            if tz:
                out = (round(lat, 4), round(lon, 4), tz)
    except Exception:
        out = None
    _station_cache[icao] = list(out) if out else None
    try:
        import json as _j
        import os as _os
        _os.makedirs(_os.path.dirname(_STATION_CACHE_PATH), exist_ok=True)
        _j.dump(_station_cache, open(_STATION_CACHE_PATH, "w"))
    except Exception:
        pass
    return out


def build_event(ev: dict, year_hint: int = 2026) -> Optional[TempEvent]:
    """Turn a Gamma /events row (with nested markets) into a TempEvent."""
    import json as _json
    title = ev.get("title", "")
    city = parse_city(title)
    markets = ev.get("markets", []) or []
    if not markets:
        return None
    desc = markets[0].get("description", "") or ev.get("description", "") or ""
    icao = extract_icao(desc)

    buckets: List[Bucket] = []
    unit_votes = {"F": 0, "C": 0}
    for m in markets:
        label = m.get("groupItemTitle") or m.get("question") or ""
        rng = parse_bucket_label(label)
        if rng is None:
            continue
        unit_votes[parse_unit(label)] += 1
        toks = m.get("clobTokenIds")
        if isinstance(toks, str):
            try:
                toks = _json.loads(toks)
            except _json.JSONDecodeError:
                toks = []
        prices = m.get("outcomePrices")
        if isinstance(prices, str):
            try:
                prices = _json.loads(prices)
            except _json.JSONDecodeError:
                prices = []
        yes_price = float(prices[0]) if prices else 0.0
        buckets.append(Bucket(
            token_id=str(toks[0]) if toks else "",
            label=label, lo=rng[0], hi=rng[1],
            price_yes=yes_price, market_id=str(m.get("id")),
            no_token_id=str(toks[1]) if toks and len(toks) > 1 else "",
        ))
    if not buckets:
        return None
    unit = "C" if unit_votes["C"] > unit_votes["F"] else "F"

    if icao and icao in STATIONS:
        lat, lon, tz = STATIONS[icao]
    elif icao and AUTO_RESOLVE:
        r = resolve_station(icao)          # any station Polymarket names -> METAR
        if r:
            lat, lon, tz = r
        else:
            g = geocode(city)
            if not g:
                return None
            lat, lon, tz = g
    else:
        g = geocode(city)
        if not g:
            return None
        lat, lon, tz = g

    kind = "low" if "lowest" in title.lower() else "high"
    return TempEvent(
        event_id=str(ev.get("id")), city=city,
        day=parse_day(title, ev.get("slug", ""), year_hint),
        unit=unit, icao=icao, lat=lat, lon=lon, tz=tz,
        buckets=sorted(buckets, key=lambda b: (b.lo, b.hi)),
        closed=bool(ev.get("closed")), kind=kind,
    )
