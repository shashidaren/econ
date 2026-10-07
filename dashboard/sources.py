"""Data fetchers — free, key-less public endpoints with multi-source fallbacks.

Every function returns plain Python data and raises on failure; the cache
layer in app.py decides how to handle errors (serve stale data, show a badge).

Why multi-source + curl -4 transport?
  On Debian LXC containers, Python's stdlib urllib.request (HTTP/1.1 only,
  no Happy Eyeballs IPv4 fallback, OpenSSL default JA3) can get tarpitted by
  Akamai (fred.stlouisfed.org), blocked by Cloudflare (stooq.com), or 429'd
  by Yahoo Finance when using a custom bot User-Agent. Using curl -4 (HTTP/2,
  IPv4-forced, browser UA) plus independent fallback providers per panel keeps
  every section resilient.

Sources & Fallbacks:
  * Market quotes   Yahoo Finance (query2/query1 v8 chart) -> FRED CSV ->
                    Stooq (.com / .pl) -> CoinGecko (PAXG for Gold)
  * FX rates        Frankfurter ECB (api.frankfurter.app / api.frankfurter.dev)
  * Macro annual    World Bank (api.worldbank.org)
  * Policy & curve  FRED fredgraph.csv (with &cosd= date window) ->
                    NY Fed EFFR JSON (DFF), ECB Data Portal CSV (ECBDFR),
                    US Treasury Daily Par/Real Yield Curve CSV (T10Y2Y, T10YIE)
"""

import csv
import gzip
import io
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,text/csv,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 12
CURL_BIN = shutil.which("curl")

# Symbol mappings for market quote fallbacks
YAHOO_MAP = {
    "^spx": "^GSPC",
    "^ndx": "^NDX",
    "^dji": "^DJI",
    "^stx": "^STOXX50E",
    "^dax": "^GDAXI",
    "^ukx": "^FTSE",
    "^nkx": "^N225",
    "^shc": "000001.SS",
    "^hsi": "^HSI",
    "cl.f": "CL=F",
    "cb.f": "BZ=F",
    "xauusd": "GC=F",
    "xagusd": "SI=F",
    "hg.f": "HG=F",
    "zw.f": "ZW=F",
}

# (FRED series_id, max_age_days before card shows a "stale" warning)
FRED_QUOTE_MAP = {
    "^spx": ("SP500", 10),
    "^ndx": ("NASDAQ100", 10),
    "^dji": ("DJIA", 10),
    "^nkx": ("NIKKEI225", 10),
    "cl.f": ("DCOILWTI", 10),
    "cb.f": ("DCOILBRENTEU", 10),
    "hg.f": ("PCOPPUSDM", 55),
    "zw.f": ("PWHEAMTUSDM", 55),
}

STOOQ_ALIASES = {
    "^stx": ["^sx5e", "^stx"],
}

# Tracks which provider last served a given symbol -> shown on the card footer
_LAST_SOURCE: dict[str, tuple[str, int | None]] = {}
_YAHOO_LOCK = threading.Lock()
_YAHOO_LAST_AT = 0.0


# --------------------------------------------------------------------------
# HTTP transport (curl -4 HTTP/2 primary, IPv4 urllib fallback)
# --------------------------------------------------------------------------

def _curl_get(url: str, timeout: int = TIMEOUT, headers: dict | None = None) -> str:
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    cmd = [
        CURL_BIN,
        "-4",                       # Force IPv4 (avoids broken IPv6 routing in LXC)
        "-g",                       # Disable URL globbing for ^, [], {}
        "-fsSL",                    # Fail on HTTP >=400, silent, follow redirects
        "--compressed",             # Handle gzip/deflate/br transparently
        "--connect-timeout", "5",
        "--max-time", str(timeout),
        "-A", hdrs["User-Agent"],
        "-H", f"Accept: {hdrs['Accept']}",
        "-H", f"Accept-Language: {hdrs['Accept-Language']}",
    ]
    for k, v in (headers or {}).items():
        if k.lower() not in ("user-agent", "accept", "accept-language"):
            cmd.extend(["-H", f"{k}: {v}"])
    cmd.append(url)
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout + 3,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or f"curl exit {proc.returncode}"
        raise RuntimeError(err)
    return proc.stdout.decode("utf-8", "replace")


def _urllib_get(url: str, timeout: int = TIMEOUT, headers: dict | None = None) -> str:
    hdrs = {**DEFAULT_HEADERS, "Accept-Encoding": "gzip", **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def _get(url: str, timeout: int = TIMEOUT, headers: dict | None = None) -> str:
    if CURL_BIN:
        try:
            return _curl_get(url, timeout=timeout, headers=headers)
        except Exception as curl_exc:
            try:
                return _urllib_get(url, timeout=timeout, headers=headers)
            except Exception:
                raise curl_exc
    return _urllib_get(url, timeout=timeout, headers=headers)


def _get_json(url: str, timeout: int = TIMEOUT, headers: dict | None = None):
    return json.loads(_get(url, timeout=timeout, headers=headers))


# Prefer IPv4 in Python's socket resolution as well so urllib never hangs on
# unrouted IPv6 addresses inside LXC containers.
_ORIG_GETADDRINFO = socket.getaddrinfo


def _ipv4_first_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    res = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    v6 = [r for r in res if r[0] != socket.AF_INET]
    return v4 + v6


socket.getaddrinfo = _ipv4_first_getaddrinfo


# --------------------------------------------------------------------------
# Market quotes: Yahoo Finance + FRED + Stooq + CoinGecko
# --------------------------------------------------------------------------

def _yahoo_history(symbol: str, days: int = 150):
    """Fetch daily closes from Yahoo Finance v8 chart API."""
    global _YAHOO_LAST_AT
    ysym = YAHOO_MAP.get(symbol, symbol)
    enc = urllib.parse.quote(ysym, safe="")
    rng = "6mo" if days <= 130 else "1y"

    with _YAHOO_LOCK:
        wait = 0.45 - (time.time() - _YAHOO_LAST_AT)
        if wait > 0:
            time.sleep(wait)
        _YAHOO_LAST_AT = time.time()

    last_err = None
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        url = (
            f"https://{host}/v8/finance/chart/{enc}"
            f"?range={rng}&interval=1d&includePrePost=false"
        )
        try:
            j = _get_json(url, timeout=10)
            result = ((j.get("chart") or {}).get("result") or [None])[0]
            if not result:
                continue
            ts_list = result.get("timestamp") or []
            quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quotes.get("close") or []
            out = []
            for ts, c in zip(ts_list, closes):
                if ts is None or c is None:
                    continue
                v = float(c)
                if v > 0:
                    d = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
                    out.append((d, round(v, 4)))
            if out:
                # Deduplicate same-date entries (keep latest)
                dedup = dict(out)
                return sorted(dedup.items())[-days:]
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"yahoo({ysym}): {last_err or 'empty'}")


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
    out.sort(key=lambda x: x[0])
    return out


def _stooq_raw_history(symbol: str, days: int = 150):
    """Try Stooq .com / .pl CSV endpoints with URL-encoded symbol."""
    d1 = (date.today() - timedelta(days=int(days * 1.6))).strftime("%Y%m%d")
    d2 = date.today().strftime("%Y%m%d")
    candidates = STOOQ_ALIASES.get(symbol, [symbol])
    last_err = None
    for cand in candidates:
        enc = urllib.parse.quote(cand, safe="")
        for domain in ("stooq.com", "stooq.pl"):
            url = f"https://{domain}/q/d/l/?s={enc}&d1={d1}&d2={d2}&i=d"
            try:
                text = _get(url, timeout=8)
                hist = _parse_stooq_csv(text)
                if hist:
                    return hist[-days:]
            except Exception as exc:
                last_err = exc
    raise RuntimeError(f"stooq({symbol}): {last_err or 'no data'}")


def _fred_quote_history(symbol: str, days: int = 150):
    """Fetch quote history from FRED public CSV for supported indices/commodities."""
    if symbol not in FRED_QUOTE_MAP:
        raise RuntimeError(f"fred: unmapped symbol {symbol}")
    sid, _ = FRED_QUOTE_MAP[symbol]
    # Monthly IMF series (PCOPPUSDM, PWHEAMTUSDM) need a longer window for sparklines
    years = 4 if sid.endswith("USDM") else 1
    rows = _fred_csv_series(sid, years=years)
    valid = [(d, v) for d, v in rows if v is not None and v > 0]
    if not valid:
        raise RuntimeError(f"fred({sid}): no valid observations")
    return valid[-days:]


def _coingecko_gold_history(days: int = 90):
    """Fallback for XAU/USD using PAX Gold (1 PAXG = 1 troy oz London Good Delivery gold)."""
    url = (
        f"https://api.coingecko.com/api/v3/coins/pax-gold/market_chart"
        f"?vs_currency=usd&days={min(days, 90)}&interval=daily"
    )
    j = _get_json(url, timeout=10)
    prices = j.get("prices") or []
    dedup = {}
    for item in prices:
        if len(item) >= 2 and item[1]:
            d = datetime.fromtimestamp(item[0] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            dedup[d] = round(float(item[1]), 2)
    out = sorted(dedup.items())
    if not out:
        raise RuntimeError("coingecko(pax-gold): no data")
    return out


def stooq_history(symbol: str, days: int = 150):
    """Multi-source market history: Yahoo -> FRED -> Stooq (-> CoinGecko for Gold).

    Kept under the name `stooq_history` so app.py and demo.py share a single interface.
    """
    errs = []
    providers = [("yahoo", lambda: _yahoo_history(symbol, days=days))]

    if symbol in FRED_QUOTE_MAP:
        sid, max_age = FRED_QUOTE_MAP[symbol]
        providers.append((f"fred:{sid}", lambda: _fred_quote_history(symbol, days=days)))

    providers.append(("stooq", lambda: _stooq_raw_history(symbol, days=days)))

    if symbol == "xauusd":
        providers.append(("coingecko", lambda: _coingecko_gold_history(days=days)))

    for src_name, fn in providers:
        try:
            hist = fn()
            if hist:
                max_age = FRED_QUOTE_MAP[symbol][1] if (
                    src_name.startswith("fred:") and symbol in FRED_QUOTE_MAP
                ) else 14
                _LAST_SOURCE[symbol] = (src_name, max_age)
                return hist
        except Exception as exc:
            errs.append(f"{src_name}: {exc}")

    raise RuntimeError(f"all sources failed -> {' | '.join(errs)}")


def quote_from_history(name: str, hist, source: str | None = None,
                       max_age_days: int | None = None,
                       symbol: str | None = None):
    """Build a display quote dict from a [(date, close)] history."""
    if not hist:
        return None
    close = hist[-1][1]
    prev = hist[-2][1] if len(hist) >= 2 else close
    chg = close - prev
    pct = (chg / prev * 100.0) if prev else 0.0
    last_date = hist[-1][0]

    src_info = _LAST_SOURCE.get(symbol or "") or _LAST_SOURCE.get(name)
    resolved_source = source or (src_info[0] if src_info else "market")
    resolved_max_age = max_age_days if max_age_days is not None else (
        src_info[1] if src_info else 14
    )

    stale = False
    if resolved_max_age and last_date:
        try:
            age = (date.today() - date.fromisoformat(last_date[:10])).days
            stale = age > resolved_max_age
        except ValueError:
            stale = False

    return {
        "name": name,
        "close": close,
        "chg": chg,
        "chg_pct": pct,
        "spark": [c for _, c in hist[-60:]],
        "date": last_date,
        "source": resolved_source,
        "stale": stale,
    }


def fred_quote(label: str, series_id: str, max_age_days: int = 10):
    """Direct FRED quote helper (Plan B compatibility)."""
    years = 4 if max_age_days > 30 else 1
    rows = fred_series(series_id, years=years)
    valid = [(d, v) for d, v in rows if v is not None and v > 0]
    if not valid:
        raise RuntimeError(f"fred_quote({series_id}): no valid observations")
    return quote_from_history(
        label,
        valid[-60:],
        source=f"FRED {series_id}",
        max_age_days=max_age_days,
    )


# --------------------------------------------------------------------------
# Frankfurter (ECB) — FX, USD base
# --------------------------------------------------------------------------

def fx_latest(currencies, base: str = "USD"):
    curs = ",".join(currencies)
    last_err = None
    for url in (
        f"https://api.frankfurter.app/latest?from={base}&to={curs}",
        f"https://api.frankfurter.dev/v1/latest?base={base}&symbols={curs}",
    ):
        try:
            j = _get_json(url, timeout=10)
            return {"base": base, "date": j["date"], "rates": j["rates"]}
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"fx_latest: {last_err}")


def fx_series(currencies, days: int = 90, base: str = "USD"):
    """Return {CUR: [(date, rate)]} oldest→newest."""
    start = (date.today() - timedelta(days=days)).isoformat()
    curs = ",".join(currencies)
    last_err = None
    for url in (
        f"https://api.frankfurter.app/{start}..?from={base}&to={curs}",
        f"https://api.frankfurter.dev/v1/{start}..?base={base}&symbols={curs}",
    ):
        try:
            j = _get_json(url, timeout=10)
            dates = sorted(j["rates"])
            return {
                cur: [(d, j["rates"][d][cur]) for d in dates if cur in j["rates"][d]]
                for cur in currencies
            }
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"fx_series: {last_err}")


# --------------------------------------------------------------------------
# World Bank — annual aggregates
# --------------------------------------------------------------------------

def worldbank_indicator(country_codes, indicator: str):
    """Latest non-empty value per country. country_codes: ISO2 list (e.g. ['US','EMU'])."""
    ids = ";".join(country_codes)
    url = (
        f"https://api.worldbank.org/v2/country/{ids}/indicator/{indicator}"
        f"?format=json&mrnev=1&per_page=100"
    )
    j = _get_json(url, timeout=12)
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
# FRED + Official Central-Bank / US Treasury Fallbacks
# --------------------------------------------------------------------------

def _fred_csv_series(series_id: str, years: int | None = None):
    """Fetch [(date, value|None)] from FRED's public fredgraph.csv with &cosd= window."""
    eff_years = years or 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * eff_years + 15)).strftime("%Y-%m-%d")
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={cutoff}"
    text = _get(url, timeout=12)
    out = []
    reader = csv.reader(io.StringIO(text))
    next(reader, None)  # skip CSV header
    for row in reader:
        if len(row) < 2:
            continue
        d, raw = row[0].strip(), row[-1].strip()
        if not d or d.startswith("<"):  # guard against HTML error page
            continue
        try:
            v = float(raw)
        except ValueError:
            v = None
        out.append((d[:10], v))
    if years:
        strict_cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * years)).strftime("%Y-%m-%d")
        out = [x for x in out if x[0] >= strict_cutoff]
    if not any(v is not None for _, v in out):
        raise RuntimeError(f"fred: no data for {series_id}")
    return out


def _nyfed_effr_series(years: int | None = None):
    """Official NY Fed Markets API fallback for US Fed Funds Rate (DFF / EFFR)."""
    limit = min(500, max(120, int((years or 1) * 260)))
    url = f"https://markets.newyorkfed.org/api/rates/unsecured/effr/last/{limit}.json"
    j = _get_json(url, timeout=10)
    rates = j.get("refRates") or []
    out = []
    for r in rates:
        d = (r.get("effectiveDate") or "")[:10]
        val = r.get("percentRate")
        if d and val is not None:
            out.append((d, float(val)))
    out.sort(key=lambda x: x[0])
    if not out:
        raise RuntimeError("nyfed: no EFFR data")
    return out


def _ecb_dfr_series(years: int | None = None):
    """Official ECB Data Portal API fallback for ECB Deposit Facility Rate (ECBDFR)."""
    obs = min(1000, max(180, int((years or 1) * 365)))
    url = (
        "https://data-api.ecb.europa.eu/service/data/FM/"
        f"B.U2.EUR.4F.KR.DFR.LEV?format=csvdata&lastNObservations={obs}"
    )
    text = _get(url, timeout=10)
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        d = (row.get("TIME_PERIOD") or "").strip()
        raw = (row.get("OBS_VALUE") or "").strip()
        if not d or not raw:
            continue
        try:
            out.append((d[:10], float(raw)))
        except ValueError:
            continue
    out.sort(key=lambda x: x[0])
    if not out:
        raise RuntimeError("ecb: no DFR data")
    return out


def _ustreasury_csv_year(year: int, kind: str = "daily_treasury_yield_curve"):
    """Fetch one calendar year of official US Treasury yield curve CSV rows."""
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all?type={kind}"
        f"&field_tdr_date_value={year}&page&_format=csv"
    )
    text = _get(url, timeout=12)
    return list(csv.DictReader(io.StringIO(text)))


def _parse_us_date(raw: str) -> str | None:
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _ustreasury_curve_series(years: int | None = None):
    """Official US Treasury fallback for T10Y2Y (10 Yr minus 2 Yr par yield spread)."""
    eff_years = max(1, min(years or 4, 4))
    cur_year = date.today().year
    # Fetch last 2 years from home.treasury.gov for fast response
    fetch_years = sorted({cur_year - 1, cur_year})
    out = {}
    for yr in fetch_years:
        try:
            for row in _ustreasury_csv_year(yr, "daily_treasury_yield_curve"):
                d = _parse_us_date(row.get("Date") or "")
                y10 = row.get("10 Yr")
                y2 = row.get("2 Yr")
                if d and y10 and y2:
                    try:
                        out[d] = round(float(y10) - float(y2), 2)
                    except ValueError:
                        continue
        except Exception:
            continue
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * eff_years)).strftime("%Y-%m-%d")
    series = [(d, v) for d, v in sorted(out.items()) if d >= cutoff]
    if not series:
        raise RuntimeError("ustreasury: no 10Y-2Y curve data")
    return series


def _ustreasury_breakeven_series(years: int | None = None):
    """Official US Treasury fallback for T10YIE (Nominal 10Y minus TIPS Real 10Y)."""
    cur_year = date.today().year
    nom_10y = {}
    real_10y = {}
    for yr in (cur_year - 1, cur_year):
        try:
            for row in _ustreasury_csv_year(yr, "daily_treasury_yield_curve"):
                d = _parse_us_date(row.get("Date") or "")
                v = row.get("10 Yr")
                if d and v:
                    nom_10y[d] = float(v)
            for row in _ustreasury_csv_year(yr, "daily_treasury_real_yield_curve"):
                d = _parse_us_date(row.get("Date") or "")
                v = row.get("10 Yr")
                if d and v:
                    real_10y[d] = float(v)
        except Exception:
            continue
    eff_years = years or 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * eff_years)).strftime("%Y-%m-%d")
    out = [
        (d, round(nom_10y[d] - real_10y[d], 2))
        for d in sorted(set(nom_10y) & set(real_10y))
        if d >= cutoff
    ]
    if not out:
        raise RuntimeError("ustreasury: no 10Y breakeven data")
    return out


_FRED_FALLBACKS = {
    "DFF": _nyfed_effr_series,
    "ECBDFR": _ecb_dfr_series,
    "T10Y2Y": _ustreasury_curve_series,
    "T10YIE": _ustreasury_breakeven_series,
}


def fred_series(series_id: str, years: int | None = None):
    """Return [(date 'YYYY-MM-DD', value|None)] oldest→newest.

    Tries FRED public CSV first (with &cosd= window so responses are small),
    then falls back to official NY Fed / ECB / US Treasury endpoints if FRED
    times out or is blocked.
    """
    errs = []
    try:
        return _fred_csv_series(series_id, years=years)
    except Exception as exc:
        errs.append(f"fred({series_id}): {exc}")

    fb = _FRED_FALLBACKS.get(series_id)
    if fb is not None:
        try:
            return fb(years=years)
        except Exception as exc:
            errs.append(f"fallback({series_id}): {exc}")

    raise RuntimeError(" | ".join(errs))


if __name__ == "__main__":
    # Quick CLI diagnostic for the server: python3 /opt/econ/dashboard/sources.py
    print(f"curl binary: {CURL_BIN or 'not found (using urllib)'}")
    checks = [
        ("fx_latest", lambda: fx_latest(["EUR", "JPY", "GBP"])),
        ("worldbank_cpi", lambda: worldbank_indicator(["US", "EMU"], "FP.CPI.TOTL.ZG")),
        ("rate:DFF", lambda: fred_series("DFF", years=1)[-1]),
        ("rate:ECBDFR", lambda: fred_series("ECBDFR", years=1)[-1]),
        ("curve:T10Y2Y", lambda: fred_series("T10Y2Y", years=1)[-1]),
        ("breakeven:T10YIE", lambda: fred_series("T10YIE", years=1)[-1]),
        ("quote:^spx", lambda: stooq_history("^spx", days=30)[-1]),
        ("quote:cl.f", lambda: stooq_history("cl.f", days=30)[-1]),
        ("quote:xauusd", lambda: stooq_history("xauusd", days=30)[-1]),
    ]
    for label, fn in checks:
        t0 = time.time()
        try:
            res = fn()
            print(f"  [OK]   {label:18s} ({time.time() - t0:.2f}s) -> {res}")
        except Exception as e:
            print(f"  [FAIL] {label:18s} ({time.time() - t0:.2f}s) -> {e}")
