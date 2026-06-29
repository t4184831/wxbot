"""Central config: endpoints, costs, and the station registry.

The single most important fact about these markets: each one resolves off a
*specific weather station* named in the market description (a Wunderground URL
ending in an ICAO code, e.g. .../us/tx/austin/KAUS). To price a market we must
pull weather for THAT station's lat/lon — not the city centre. Getting the
station wrong is the fastest way to lose money here.
"""
from __future__ import annotations
from dataclasses import dataclass

# --- Polymarket endpoints ---
GAMMA = "https://gamma-api.polymarket.com"      # market/event metadata
CLOB = "https://clob.polymarket.com"            # order books, prices, history
DATA_API = "https://data-api.polymarket.com"    # trades, positions, holders
PNL_API = "https://user-pnl-api.polymarket.com" # cumulative user pnl

# --- Weather endpoints (all free, no key) ---
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"
OM_ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"   # historical reanalysis
OM_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"

HEADERS = {"User-Agent": "wxbot/0.1 (research)"}


@dataclass
class Costs:
    """Real friction of trading these markets. CLOB taker fee is ~0 today, but you
    still cross the spread and eat slippage. Modeled in price (probability) units."""
    taker_fee: float = 0.0
    friction_per_share: float = 0.005   # half a cent — cross the spread
    # require this much model-vs-price edge (in $/share) before flagging a trade
    min_edge: float = 0.04
    # never buy a bucket priced above this — risk/reward is terrible (win pennies,
    # lose the lot). The target trader's NO-buys cluster at 0.99; his YES-buys ~0.92.
    max_buy_price: float = 0.97
    # ...and never chase a longshot the market prices as near-dead. If the whole book
    # says ~0 and our model says ~1, WE are wrong (stale market / wrong station), not
    # the crowd. The trader buys plausible favorites, not 0.001 lottery tickets.
    min_buy_price: float = 0.20
    # reject implausibly large "edges" — a model-vs-market gap this big is a data
    # mismatch, not free money.
    max_edge: float = 0.45

    # --- NO-scalp (the trader's actual main game) ---
    # Buy NO on buckets the observed high has ALREADY passed (so they cannot be the
    # day's high -> NO is worth ~1.00), riding the last cents to resolution.
    # margin in whole degrees below the observed high before a bucket counts "dead";
    # =1 matches his trades (incl. the just-crossed bucket) but carries boundary
    # data-risk; =2 is safer/deeper.
    no_scalp_margin: int = 1
    # only act when the market AGREES it's near-dead (NO already rich). If NO is cheap
    # on a bucket we call dead, our station data disagrees with the market -> skip.
    no_scalp_min_price: float = 0.90
    no_scalp_max_price: float = 0.995   # leave room to 0.999/1.00
    no_scalp_min_edge: float = 0.005    # net of friction


@dataclass
class ModelConfig:
    # station-vs-grid bias + rounding uncertainty. The Open-Meteo grid cell is not
    # the exact tarmac sensor; this floor on sigma keeps the model honest and is the
    # main guard against overconfident -90c losses.
    sigma_floor_c: float = 1.1          # ~2 F of irreducible uncertainty
    # local hour by which we assume "high mostly in" for intraday mode
    peak_hour_local: int = 16
    # minimum favorite-bucket probability to consider a directional buy
    min_fav_prob: float = 0.80


# ICAO -> (lat, lon, tz). The trader's actual city rotation, keyed by the station
# named in each market's resolution text. Extend via Open-Meteo geocoding fallback.
STATIONS = {
    "WSSS": (1.3644, 103.9915, "Asia/Singapore"),     # Singapore Changi
    "LIML": (45.4450, 9.2767, "Europe/Rome"),          # Milan Linate
    "EDDM": (48.3538, 11.7861, "Europe/Berlin"),       # Munich
    "LTAC": (40.1281, 32.9951, "Europe/Istanbul"),     # Ankara Esenboga
    "LTFM": (41.2753, 28.7519, "Europe/Istanbul"),     # Istanbul
    "EHAM": (52.3105, 4.7683, "Europe/Amsterdam"),     # Amsterdam Schiphol
    "KAUS": (30.1975, -97.6664, "America/Chicago"),    # Austin-Bergstrom
    "KIAH": (29.9902, -95.3368, "America/Chicago"),    # Houston Intercontinental
    "RPLL": (14.5086, 121.0197, "Asia/Manila"),        # Manila
    "KSEA": (47.4502, -122.3088, "America/Los_Angeles"),
    "KDFW": (32.8998, -97.0403, "America/Chicago"),
    "KATL": (33.6407, -84.4277, "America/New_York"),
    "LEMD": (40.4719, -3.5626, "Europe/Madrid"),
    "KSFO": (37.6213, -122.3790, "America/Los_Angeles"),
    "KLAX": (33.9416, -118.4085, "America/Los_Angeles"),
    "EGLL": (51.4700, -0.4543, "Europe/London"),       # London Heathrow
    "LFPG": (49.0097, 2.5479, "Europe/Paris"),         # Paris CDG
    "VHHH": (22.3080, 113.9180, "Asia/Hong_Kong"),
    "UUEE": (55.9726, 37.4146, "Europe/Moscow"),       # Moscow Sheremetyevo
    "KNYC": (40.7790, -73.9690, "America/New_York"),   # NYC Central Park
    # --- expanded coverage (primary METAR airport per city) ---
    "KORD": (41.9786, -87.9048, "America/Chicago"),    # Chicago O'Hare
    "KMDW": (41.7860, -87.7524, "America/Chicago"),    # Chicago Midway
    "KMIA": (25.7932, -80.2906, "America/New_York"),   # Miami
    "KPHX": (33.4342, -112.0116, "America/Phoenix"),   # Phoenix
    "KDEN": (39.8617, -104.6732, "America/Denver"),    # Denver
    "KBOS": (42.3656, -71.0096, "America/New_York"),   # Boston
    "KDCA": (38.8512, -77.0377, "America/New_York"),   # Washington DC
    "KIAD": (38.9531, -77.4565, "America/New_York"),   # Washington Dulles
    "KLAS": (36.0801, -115.1522, "America/Los_Angeles"),  # Las Vegas
    "KPDX": (45.5887, -122.5975, "America/Los_Angeles"),  # Portland OR
    "KPHL": (39.8719, -75.2411, "America/New_York"),   # Philadelphia
    "KMSP": (44.8820, -93.2218, "America/Chicago"),    # Minneapolis
    "KSAN": (32.7338, -117.1933, "America/Los_Angeles"),  # San Diego
    "KMCO": (28.4312, -81.3081, "America/New_York"),   # Orlando
    "KSLC": (40.7884, -111.9779, "America/Denver"),    # Salt Lake City
    "KBNA": (36.1245, -86.6782, "America/Chicago"),    # Nashville
    "KDAL": (32.8471, -96.8518, "America/Chicago"),    # Dallas Love
    "KSTL": (38.7487, -90.3700, "America/Chicago"),    # St. Louis
    "KCLT": (35.2140, -80.9431, "America/New_York"),   # Charlotte
    "KTPA": (27.9755, -82.5332, "America/New_York"),   # Tampa
    "KSMF": (38.6954, -121.5908, "America/Los_Angeles"),  # Sacramento
    "RJTT": (35.5523, 139.7798, "Asia/Tokyo"),         # Tokyo Haneda
    "RJAA": (35.7647, 140.3863, "Asia/Tokyo"),         # Tokyo Narita
    "RKSI": (37.4691, 126.4505, "Asia/Seoul"),         # Seoul Incheon
    "YSSY": (-33.9461, 151.1772, "Australia/Sydney"),  # Sydney
    "YMML": (-37.6733, 144.8433, "Australia/Melbourne"),  # Melbourne
    "CYYZ": (43.6772, -79.6306, "America/Toronto"),    # Toronto
    "MMMX": (19.4361, -99.0719, "America/Mexico_City"),  # Mexico City
    "LIRF": (41.8003, 12.2389, "Europe/Rome"),         # Rome Fiumicino
    "EDDF": (50.0379, 8.5622, "Europe/Berlin"),        # Frankfurt
    "EDDB": (52.3667, 13.5033, "Europe/Berlin"),       # Berlin
    "LEBL": (41.2971, 2.0785, "Europe/Madrid"),        # Barcelona
    "OMDB": (25.2528, 55.3644, "Asia/Dubai"),          # Dubai
    "VIDP": (28.5562, 77.1000, "Asia/Kolkata"),        # Delhi
    "VABB": (19.0887, 72.8679, "Asia/Kolkata"),        # Mumbai
    "VTBS": (13.6811, 100.7472, "Asia/Bangkok"),       # Bangkok
    "WMKK": (2.7456, 101.7099, "Asia/Kuala_Lumpur"),   # Kuala Lumpur
    "HECA": (30.1219, 31.4056, "Africa/Cairo"),        # Cairo
    "SBGR": (-23.4356, -46.4731, "America/Sao_Paulo"),  # Sao Paulo
    "SAEZ": (-34.8222, -58.5358, "America/Argentina/Buenos_Aires"),  # Buenos Aires
    "FAOR": (-26.1337, 28.2420, "Africa/Johannesburg"),  # Johannesburg
    "ZBAA": (40.0801, 116.5846, "Asia/Shanghai"),      # Beijing
    "ZSPD": (31.1434, 121.8052, "Asia/Shanghai"),      # Shanghai Pudong
    "LOWW": (48.1103, 16.5697, "Europe/Vienna"),       # Vienna
    "LSZH": (47.4647, 8.5492, "Europe/Zurich"),        # Zurich
    "EKCH": (55.6180, 12.6508, "Europe/Copenhagen"),   # Copenhagen
    "ESSA": (59.6519, 17.9186, "Europe/Stockholm"),    # Stockholm
    "EIDW": (53.4213, -6.2701, "Europe/Dublin"),       # Dublin
    "LPPT": (38.7742, -9.1342, "Europe/Lisbon"),       # Lisbon
    "LGAV": (37.9364, 23.9445, "Europe/Athens"),       # Athens
    "LTFJ": (40.8986, 29.3092, "Europe/Istanbul"),     # Istanbul Sabiha
}

# cities the target trader concentrates in, ranked by his realized edge / stability.
# stable low-variance climates first — easiest to forecast, least bot competition.
PREFERRED_CITIES = ["Singapore", "Manila", "Milan", "Munich", "Madrid", "Ankara"]

COSTS = Costs()
MODEL = ModelConfig()
