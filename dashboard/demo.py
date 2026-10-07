"""Demo data — same function signatures as sources.py, but returns stable
synthetic data so the UI can be previewed with zero network access.

Enabled with ECON_DEMO=1. On the server, run WITHOUT it for live data.
"""

import math
import zlib
from datetime import date, timedelta

# Plausible starting levels so the demo looks realistic
_BASE = {
    "^spx": 5735.0, "^ndx": 20180.0, "^dji": 42140.0, "^stx": 4960.0,
    "^dax": 18720.0, "^ukx": 8250.0, "^nkx": 38900.0, "^shc": 3065.0, "^hsi": 19630.0,
    "cl.f": 71.4, "cb.f": 75.2, "xauusd": 2412.0, "xagusd": 30.4,
    "hg.f": 4.31, "zw.f": 577.0,
}
_NAME = dict()
_FX = {"EUR": 0.862, "JPY": 148.2, "CNY": 7.06, "GBP": 0.772,
       "AUD": 1.517, "CHF": 0.851, "INR": 83.9, "MYR": 4.21}
_WB = {
    "FP.CPI.TOTL.ZG": [("United States", "US", "2.9"), ("Euro area", "EMU", "2.4"),
                       ("China", "CN", "0.2"), ("Japan", "JP", "2.5"),
                       ("United Kingdom", "GB", "2.6"), ("India", "IN", "4.9"),
                       ("Germany", "DE", "2.5")],
    "NY.GDP.MKTP.KD.ZG": [("United States", "US", "2.8"), ("Euro area", "EMU", "0.9"),
                          ("China", "CN", "5.0"), ("Japan", "JP", "0.1"),
                          ("United Kingdom", "GB", "1.1"), ("India", "IN", "6.7"),
                          ("Germany", "DE", "-0.3")],
}
_FRED_LEVEL = {"DFF": 4.83, "ECBDFR": 3.50, "T10Y2Y": -0.05, "T10YIE": 2.28}


def _seed(text: str) -> int:
    return zlib.crc32(text.encode())


def _walk(seed_text: str, base: float, n: int, vol=0.009, drift=0.0004):
    """Deterministic pseudo random walk ending today."""
    state = _seed(seed_text)
    out = []
    v = base
    today = date.today()
    for i in range(n):
        # xorshift-ish step so demo data is stable but not smooth
        state ^= (state << 13) & 0xFFFFFFFF
        state ^= state >> 17
        state ^= (state << 5) & 0xFFFFFFFF
        r = (state % 20000) / 20000.0 - 0.5          # -0.5..0.5
        wave = math.sin(i / 9.0 + _seed(seed_text) % 7) * 0.35
        v *= 1.0 + drift + (r + wave) * vol
        out.append(((today - timedelta(days=n - 1 - i)).isoformat(), round(v, 4)))
    return out


def _dates(n):
    today = date.today()
    return [(today - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def stooq_history(symbol: str, days: int = 150):
    base = _BASE.get(symbol, 100.0 + (_seed(symbol) % 900) / 7.0)
    return _walk(symbol, base, days)


def fx_latest(currencies, base: str = "USD"):
    rates = {}
    for c in currencies:
        r = _FX.get(c)
        rates[c] = round(r * (1 + ((_seed(c) % 100) - 50) / 5000.0), 4) if r else 1.0
    return {"base": base, "date": date.today().isoformat(), "rates": rates}


def fx_series(currencies, days: int = 90, base: str = "USD"):
    out = {}
    dates = _dates(days)
    for c in currencies:
        r = _FX.get(c, 1.0)
        walk = _walk("fx" + c, r, days, vol=0.004, drift=0.0)
        out[c] = list(zip(dates, [v for _, v in walk]))
    return out


def worldbank_indicator(country_codes, indicator: str):
    iso2_to3 = {"US": "USA", "EMU": "EMU", "CN": "CHN", "JP": "JPN",
                "GB": "GBR", "IN": "IND", "DE": "DEU"}
    rows = _WB.get(indicator, [])
    out = []
    for name, c2, val in rows:
        if c2 in country_codes:
            out.append({"code": iso2_to3[c2], "name": name,
                        "year": str(date.today().year - 2),
                        "value": round(float(val), 2)})
    out.sort(key=lambda x: -x["value"])
    return out


def fred_series(series_id: str, years: int | None = None):
    n = 365 * (years or 1)
    base = _FRED_LEVEL.get(series_id, 1.0)
    vol = 0.012 if series_id in ("T10Y2Y", "T10YIE") else 0.003
    return _walk(series_id, base, n, vol=vol, drift=0.0)
