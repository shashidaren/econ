"""Demo data — same function signatures as sources.py, but returns stable
synthetic data so the UI can be previewed with zero network access.

Enabled with ECON_DEMO=1. On the server, run WITHOUT it for live data.
"""

import math
import zlib
from datetime import date, datetime, timedelta, timezone

# Plausible starting levels so the demo looks realistic
_BASE = {
    "^spx": 5735.0, "^ndx": 20180.0, "^dji": 42140.0, "^stx": 4960.0,
    "^dax": 18720.0, "^ukx": 8250.0, "^nkx": 38900.0, "^shc": 3065.0, "^hsi": 19630.0,
    "cl.f": 79.7, "cb.f": 125.4, "xauusd": 4118.9, "xagusd": 60.08,
    "hg.f": 4.31, "zw.f": 577.0, "al.f": 3158.3, "ni.f": 16632.4,
}
_NAME = dict()
_FX = {"EUR": 0.862, "JPY": 148.2, "CNY": 7.06, "GBP": 0.772,
       "AUD": 1.517, "CHF": 0.851, "INR": 83.9, "MYR": 4.21}
_WB = {
    "FP.CPI.TOTL.ZG": [("United States", "US", "2.9"), ("Euro area", "EMU", "2.4"),
                       ("China", "CN", "0.2"), ("Japan", "JP", "2.5"),
                       ("United Kingdom", "GB", "2.6"), ("India", "IN", "4.9"),
                       ("Germany", "DE", "2.5"), ("Malaysia", "MY", "2.1")],
    "NY.GDP.MKTP.KD.ZG": [("United States", "US", "2.8"), ("Euro area", "EMU", "0.9"),
                          ("China", "CN", "5.0"), ("Japan", "JP", "0.1"),
                          ("United Kingdom", "GB", "1.1"), ("India", "IN", "6.7"),
                          ("Germany", "DE", "-0.3"), ("Malaysia", "MY", "4.4")],
}
_FRED_LEVEL = {
    "DFF": 4.83, "ECBDFR": 3.50, "T10Y2Y": -0.05, "T10YIE": 2.28, "DFII10": 1.70,
    "SP500": 7801.8, "NASDAQ100": 31224.7, "NASDAQCOM": 31224.7,
    "DJIA": 51179.9, "NIKKEI225": 70035.7, "POILWTIUSDM": 79.7,
    "DCOILBRENTEU": 125.4, "POILBREUSDM": 83.7,
    "PCOPPUSDM": 13542.8, "PWHEAMTUSDM": 228.7,
    "PALUMUSDM": 3158.3, "PNICKUSDM": 16632.4,
}


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


def stock_history_batch(symbols, vendor_symbols=None, days: int = 130):
    """Synthetic watchlist histories for layout testing only."""
    out = {}
    for symbol in symbols:
        seed = _seed("stock:" + symbol)
        base = 12.0 + (seed % 3600) / 100.0
        drift = ((seed % 41) - 12) / 10000.0
        walk = _walk("stock:" + symbol, base, days, vol=0.014, drift=drift)
        volume = 900_000 + (seed % 3_000_000)
        out[symbol] = {
            "rows": [(d, close, volume) for d, close in walk],
            "source": "DEMO synthetic",
        }
    return out


def stock_quotes_batch(symbols, *, now=None):
    """Synthetic ~15-min-delayed quotes for offline preview only."""
    import sources

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment_utc = moment.astimezone(timezone.utc)
    session = sources.us_market_session(moment_utc)
    wanted = list(dict.fromkeys(
        str(s).strip().upper() for s in (symbols or []) if str(s or "").strip()
    ))
    histories = stock_history_batch(wanted, days=130) if wanted else {}
    items = {}
    for sym in wanted:
        rows = (histories.get(sym) or {}).get("rows") or []
        prev_close = float(rows[-1][1]) if rows else 20.0
        seed = _seed("quote:" + sym)
        bump_pct = ((seed % 261) - 110) / 100.0  # -1.10% .. +1.50%
        price = round(prev_close * (1.0 + bump_pct / 100.0), 2)
        change = round(price - prev_close, 2)
        change_pct = round((change / prev_close) * 100.0, 2) if prev_close else 0.0
        low = round(min(prev_close, price) * 0.992, 2)
        high = round(max(prev_close, price) * 1.008, 2)
        items[sym] = {
            "symbol": sym,
            "price": price,
            "prev_close": round(prev_close, 2),
            "change": change,
            "change_pct": change_pct,
            "open": round(prev_close * 1.001, 2),
            "day_high": high,
            "day_low": low,
            "volume": float(600_000 + (seed % 2_000_000)),
            "extended_price": None,
            "extended_change_pct": None,
            "extended_label": None,
            "as_of_utc": (moment_utc - timedelta(minutes=15)).isoformat(
                timespec="seconds"
            ),
            "as_of_display": None,
            "delayed": True,
            "source": "DEMO synthetic",
            "market_status": session["state"],
            "demo": True,
        }
    return {
        "status": "ok" if items else "waiting",
        "message": (
            "Synthetic demo quotes — not live or delayed market prices."
            if items else "No demo candidates to quote."
        ),
        "delay_note": "Demo mode: synthetic quotes generated locally; not real market data.",
        "market_session": session,
        "fetched_at_utc": moment_utc.isoformat(timespec="seconds"),
        "items": items,
        "errors": {},
        "quote_providers": [],
        "coverage": {
            "symbols": len(wanted),
            "with_quotes": len(items),
            "providers": ["DEMO synthetic"] if items else [],
        },
        "demo": True,
    }


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
                "GB": "GBR", "IN": "IND", "DE": "DEU", "MY": "MYS"}
    rows = _WB.get(indicator, [])
    out = []
    for name, c2, val in rows:
        if c2 in country_codes:
            out.append({"code": iso2_to3[c2], "name": name,
                        "year": str(date.today().year - 2),
                        "value": round(float(val), 2)})
    out.sort(key=lambda x: -x["value"])
    return out


# Deterministic synthetic annual paths so the Economy 101 tab can be previewed
# offline. Values are DEMO ONLY and never claim to be live World Bank prints.
_DEMO_ANNUAL = {
    "NY.GDP.MKTP.KD.ZG": {
        "US": [2.3, 1.7, 2.4, 2.9, 2.5, 2.2, -2.2, 6.1, 2.5, 2.1, 2.9, 2.8],
        "MY": [5.4, 4.8, 4.9, 4.7, 4.4, 4.0, -5.5, 3.1, 8.7, 3.6, 4.2, 4.4],
    },
    "FP.CPI.TOTL.ZG": {
        "US": [1.6, 0.1, 1.2, 1.8, 1.9, 2.3, 1.2, 4.7, 8.0, 4.1, 2.9, 2.9],
        "MY": [2.1, 0.9, 1.0, 0.7, 1.0, 1.4, -1.1, 2.5, 3.4, 2.5, 1.8, 2.1],
    },
    "SL.UEM.TOTL.ZS": {
        "US": [6.2, 5.3, 4.9, 4.4, 3.9, 3.7, 8.1, 5.3, 3.6, 3.6, 4.0, 4.1],
        "MY": [3.2, 3.4, 3.3, 3.3, 3.3, 3.3, 4.5, 4.6, 3.8, 3.5, 3.4, 3.3],
    },
    "NE.TRD.GNFS.ZS": {
        "US": [24.4, 24.0, 24.6, 24.6, 24.5, 24.3, 23.2, 25.0, 25.9, 24.9, 24.7, 24.8],
        "MY": [133.5, 131.0, 129.4, 126.9, 123.3, 121.0, 116.0, 130.6, 143.0, 138.4,
               135.1, 136.0],
    },
}
_DEMO_NAMES = {"US": "United States", "MY": "Malaysia"}


def worldbank_history(country_codes, indicator: str, years: int = 12):
    codes = [str(c).upper() for c in country_codes or []]
    paths = _DEMO_ANNUAL.get(indicator)
    if not paths:
        raise RuntimeError(f"worldbank: no demo series for {indicator}")
    last_year = date.today().year - 2
    out = {}
    for code in codes:
        values = paths.get(code)
        if not values:
            out[code] = {"name": _DEMO_NAMES.get(code, code), "series": []}
            continue
        window = values[-max(2, int(years)):]
        start = last_year - len(window) + 1
        series = [[start + i, float(v)] for i, v in enumerate(window)]
        out[code] = {"name": _DEMO_NAMES.get(code, code), "series": series}
    return out


def fred_series(series_id: str, years: int | None = None):
    n = 365 * (years or 1)
    base = _FRED_LEVEL.get(series_id, 1.0)
    vol = 0.012 if series_id in ("T10Y2Y", "T10YIE") else 0.003
    return _walk(series_id, base, n, vol=vol, drift=0.0)


def fetch_news(candidates, *, days=7, per_symbol=3, now=None):
    """Synthetic headlines so the Stocks tab can be previewed with no network.

    Every title is prefixed DEMO and carries no link, so a demo page can never
    look like a live news feed.
    """
    now = now or datetime.now(timezone.utc)
    items, errors = {}, {}
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        symbol = str(candidate.get("symbol") or "").upper()
        name = str(candidate.get("name") or symbol)
        if not symbol:
            continue
        items[symbol] = [{
            "title": (f"DEMO headline {i + 1}: synthetic {name} story "
                      "for layout testing only"),
            "link": None,
            "publisher": "DEMO synthetic",
            "published_utc": (now - timedelta(hours=7 * (i + 1))).isoformat(
                timespec="minutes"),
        } for i in range(max(1, int(per_symbol)))]
    return {
        "status": "ok" if items else "unavailable",
        "message": ("Synthetic demo headlines — not real news."
                    if items else "No demo candidates to label."),
        "window_days": days,
        "per_symbol_limit": per_symbol,
        "fetched_at_utc": now.isoformat(timespec="minutes"),
        "items": items,
        "errors": errors,
        "coverage": {"symbols": len(items), "with_headlines": len(items),
                     "headlines": sum(len(v) for v in items.values()),
                     "providers": ["DEMO synthetic"] if items else []},
        "limitations": ("Demo mode: these headlines are generated locally and "
                        "carry no links. They are not real news."),
        "demo": True,
    }
