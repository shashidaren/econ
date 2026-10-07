"""Data fetchers — free, key-less public endpoints only.

Every function returns plain Python data and raises on failure; the cache
layer in app.py decides how to handle errors (serve stale data, show a badge).

Sources:
  * Stooq           https://stooq.com          — daily OHLC CSV for indices & commodities
  * Frankfurter     https://api.frankfurter.app — ECB FX reference rates
  * World Bank      https://api.worldbank.org   — annual macro aggregates
  * FRED fredgraph  https://fred.stlouisfed.org — public CSV endpoint (no API key)
"""

import csv
import io
import json
import urllib.request
from datetime import date, datetime, timedelta

UA = {"User-Agent": "econ-dashboard/0.1 (self-hosted; +https://github.com/shashidaren/econ)"}
TIMEOUT = 15


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _get_json(url: str):
    return json.loads(_get(url))


# --------------------------------------------------------------------------
# Stooq — daily close history
# --------------------------------------------------------------------------

def _parse_stooq_csv(text: str):
    """Yield (date_str, close) from Stooq's Date,Open,High,Low,Close,Volume CSV."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            d = (row.get("Date") or "").strip()
            close = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
        if d and close > 0:
            out.append((d, close))
    return out


def stooq_history(symbol: str, days: int = 150):
    """Return [(date 'YYYY-MM-DD', close)] oldest→newest, ~`days` trading days."""
    d1 = (date.today() - timedelta(days=int(days * 1.6))).strftime("%Y%m%d")
    d2 = date.today().strftime("%Y%m%d")
    try:
        text = _get(f"https://stooq.com/q/d/l/?s={symbol}&d1={d1}&d2={d2}&i=d")
        hist = _parse_stooq_csv(text)
    except Exception:
        hist = []
    if not hist:
        # Fallback: full history, trim tail (some symbols dislike date params)
        text = _get(f"https://stooq.com/q/d/l/?s={symbol}&i=d")
        hist = _parse_stooq_csv(text)[-days:]
    if not hist:
        raise RuntimeError(f"stooq: no data for {symbol!r}")
    return hist


def quote_from_history(name: str, hist):
    """Build a display quote dict from a close history."""
    if not hist:
        return None
    close = hist[-1][1]
    prev = hist[-2][1] if len(hist) >= 2 else close
    chg = close - prev
    pct = (chg / prev * 100.0) if prev else 0.0
    return {
        "name": name,
        "close": close,
        "chg": chg,
        "chg_pct": pct,
        "spark": [c for _, c in hist[-60:]],
        "date": hist[-1][0],
    }


# --------------------------------------------------------------------------
# Frankfurter (ECB) — FX, USD base
# --------------------------------------------------------------------------

def fx_latest(currencies, base: str = "USD"):
    url = f"https://api.frankfurter.app/latest?from={base}&to={','.join(currencies)}"
    j = _get_json(url)
    return {"base": base, "date": j["date"], "rates": j["rates"]}


def fx_series(currencies, days: int = 90, base: str = "USD"):
    """Return {CUR: [(date, rate)]} oldest→newest."""
    start = (date.today() - timedelta(days=days)).isoformat()
    url = f"https://api.frankfurter.app/{start}..?from={base}&to={','.join(currencies)}"
    j = _get_json(url)
    dates = sorted(j["rates"])
    return {
        cur: [(d, j["rates"][d][cur]) for d in dates if cur in j["rates"][d]]
        for cur in currencies
    }


# --------------------------------------------------------------------------
# World Bank — annual aggregates
# --------------------------------------------------------------------------

def worldbank_indicator(country_codes, indicator: str):
    """Latest non-empty value per country. country_codes: ISO2 list (e.g. ['US','EMU'])."""
    ids = ";".join(country_codes)
    url = (f"https://api.worldbank.org/v2/country/{ids}/indicator/{indicator}"
           f"?format=json&mrnev=1&per_page=100")
    j = _get_json(url)
    rows = j[1] or []
    out = []
    for r in rows:
        if r.get("value") is None:
            continue
        out.append({
            "code": r.get("countryiso3code") or "",
            "name": r["country"]["value"],
            "year": str(r["date"]),
            "value": round(float(r["value"]), 2),
        })
    if not out:
        raise RuntimeError(f"worldbank: no data for {indicator}")
    out.sort(key=lambda x: -x["value"])
    return out


# --------------------------------------------------------------------------
# FRED — public fredgraph.csv (no API key)
# --------------------------------------------------------------------------

def fred_series(series_id: str, years: int | None = None):
    """Return [(date 'YYYY-MM-DD', value|None)] oldest→newest."""
    text = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}")
    out = []
    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # header
    for row in reader:
        if len(row) < 2:
            continue
        d, raw = row[0].strip(), row[-1].strip()
        try:
            v = float(raw)
        except ValueError:
            v = None  # FRED uses '.' for missing observations
        out.append((d[:10], v))
    if years:
        cutoff = (datetime.utcnow() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
        out = [x for x in out if x[0] >= cutoff]
    if not out:
        raise RuntimeError(f"fred: no data for {series_id}")
    return out
