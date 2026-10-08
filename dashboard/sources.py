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
  * Market quotes   Yahoo Finance (v7 spark batch / v8 chart) -> FRED CSV ->
                    Stooq (.com / .pl, https + http) -> CoinGecko (tokenised metal)
  * FX rates        Frankfurter ECB (api.frankfurter.app / api.frankfurter.dev)
  * Macro annual    World Bank (api.worldbank.org)
  * Policy & curve  FRED fredgraph.csv (with &cosd= date window) ->
                    NY Fed EFFR JSON (DFF), ECB Data Portal CSV (ECBDFR),
                    US Treasury Daily Par/Real Yield Curve CSV (T10Y2Y, T10YIE)

Provider circuit breakers
-------------------------
Every upstream is wrapped in a breaker (`provider_available` / `note_provider_*`).
A host that 429s or times out is skipped for a cooldown window instead of being
retried by all 15 quote fetchers, which turns a 45 s dead provider into ~0 ms
and stops us hammering Yahoo into a deeper rate-limit. `provider_status()`
reports the current state so the board footer can explain empty panels.

Diagnostics:
  python3 dashboard/sources.py            short self-test (one check per panel)
  python3 dashboard/sources.py --doctor   exhaustive per-provider probe
"""

import csv
import gzip
import io
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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

# Yahoo pacing: how long to wait between requests to Yahoo (seconds). Yahoo
# 429s data-centre IPs that burst, so this is deliberately slow-ish.
YAHOO_MIN_INTERVAL = float(os.environ.get("ECON_YAHOO_PACE", "1.5"))
# Grab a session cookie from fc.yahoo.com before quoting (reduces 429s).
YAHOO_USE_COOKIE = os.environ.get("ECON_YAHOO_COOKIE", "1") not in ("0", "false", "no")

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

# (scheme, host, breaker-key suffix). Shared by the fetcher and the pre-flight
# probe in warm_providers() so the two can never drift apart.
STOOQ_ENDPOINTS = (
    ("https", "stooq.com", ""),
    ("https", "stooq.pl", ""),
    ("http", "stooq.com", ":80"),
)

# CoinGecko tokenised-metal fallbacks (1 token ~= 1 troy oz).
# pax-gold is well established; kinesis-silver is best-effort — confirm with
# `python3 dashboard/sources.py --doctor` and delete the line if it misbehaves.
COINGECKO_MAP = {
    "xauusd": "pax-gold",
    "xagusd": "kinesis-silver",
}

# FRED series worth probing but NOT wired into the board until confirmed live
# (shown by `--doctor`; add to FRED_QUOTE_MAP once verified on the server).
FRED_PROBE_CANDIDATES = [
    "POILWTIUSDM",       # Global price of WTI, Monthly (IMF) — replaces dead DCOILWTI?
    "POILBREUSDM",       # Global price of Brent, Monthly (IMF) — replaces DCOILBRENTEU?
    "PSILVUSDM",         # Global price of Silver, Monthly
    "PALUMUSDM",         # Global price of Aluminum, Monthly
    "PNICKUSDM",         # Global price of Nickel, Monthly
    "GOLDAMGBD228NLBM",  # LBMA Gold Price (AM fix)
]

# Tracks which provider last served a given symbol -> shown on the card footer
_LAST_SOURCE: dict[str, tuple[str, int | None]] = {}
_YAHOO_LOCK = threading.Lock()
_YAHOO_LAST_AT = 0.0
_YAHOO_COOKIE: str | None = None
_YAHOO_COOKIE_AT = 0.0
_SPARK_CACHE: dict[str, tuple[float, list]] = {}
_SPARK_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Provider circuit breakers — a blocked host must cost milliseconds, not 45s
# --------------------------------------------------------------------------

BREAKER_THRESHOLD = 2            # consecutive failures before a cooldown starts
BREAKER_COOLDOWN = 30 * 60       # seconds to skip a failing provider
BREAKER_RATE_COOLDOWN = 15 * 60  # seconds to back off after an HTTP 429

_BREAKERS: dict[str, dict] = {}
_BREAKER_LOCK = threading.Lock()


def _classify(err) -> tuple[bool, bool]:
    """Return (unreachable, rate_limited) from a transport error message."""
    s = str(err)
    unreachable = any(t in s for t in (
        "Timeout was reached", "Failed to connect", "Connection timeout",
        "timed out", "SSL_ERROR_SYSCALL", "(28)", "(7)", "(35)", "(6)",
    ))
    return unreachable, "429" in s


def provider_available(name: str) -> bool:
    """False while `name` is inside its cooldown window."""
    with _BREAKER_LOCK:
        b = _BREAKERS.get(name)
        return not (b and b.get("until", 0.0) > time.time())


def note_provider_ok(name: str) -> None:
    """A success clears the failure memory for that provider."""
    with _BREAKER_LOCK:
        _BREAKERS.pop(name, None)


def note_provider_failure(name: str, err, *, unreachable=False,
                          rate_limited=False, cooldown=None) -> None:
    """Record a failure; start a cooldown for hard failures / rate limits."""
    if cooldown is None:
        if rate_limited:
            cooldown = BREAKER_RATE_COOLDOWN
        elif unreachable:
            cooldown = BREAKER_COOLDOWN
    with _BREAKER_LOCK:
        b = _BREAKERS.setdefault(name, {"fails": 0, "until": 0.0, "reason": "", "last": 0.0})
        b["fails"] += 1
        b["reason"] = str(err)[:200]
        b["last"] = time.time()
        if cooldown is not None:
            b["until"] = time.time() + cooldown
        elif b["fails"] >= BREAKER_THRESHOLD:
            b["until"] = time.time() + BREAKER_COOLDOWN


def provider_status() -> list[dict]:
    """Current breaker state, newest trouble first — shown in the board footer."""
    now = time.time()
    with _BREAKER_LOCK:
        items = [(n, dict(b)) for n, b in _BREAKERS.items()]
    out = []
    for name, b in items:
        left = b.get("until", 0.0) - now
        out.append({
            "provider": name,
            "state": "cooling" if left > 0 else "degraded",
            "fails": b.get("fails", 0),
            "reason": b.get("reason", ""),
            "retry_in": int(max(0, left)),
        })
    out.sort(key=lambda x: (-x["retry_in"], x["provider"]))
    return out


def reset_breakers() -> None:
    with _BREAKER_LOCK:
        _BREAKERS.clear()


# --------------------------------------------------------------------------
# HTTP transport (curl -4 HTTP/2 primary, IPv4 urllib fallback)
# --------------------------------------------------------------------------

_STATUS_MARK = "__HTTP__:"


def _curl_get(url: str, timeout: int = TIMEOUT, headers: dict | None = None,
              connect_timeout: int = 5) -> str:
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    cmd = [
        CURL_BIN,
        "-4",                       # Force IPv4 (avoids broken IPv6 routing in LXC)
        "-g",                       # Disable URL globbing for ^, [], {}
        "-fsSL",                    # Fail on HTTP >=400, silent, follow redirects
        "--compressed",             # Handle gzip/deflate/br transparently
        "--connect-timeout", str(connect_timeout),
        "--max-time", str(timeout),
        "-A", hdrs["User-Agent"],
        "-H", f"Accept: {hdrs['Accept']}",
        "-H", f"Accept-Language: {hdrs['Accept-Language']}",
    ]
    for k, v in (headers or {}).items():
        if k.lower() not in ("user-agent", "accept", "accept-language"):
            cmd.extend(["-H", f"{k}: {v}"])
    # Append the status code as a marker line so callers can tell a 429 from a
    # DNS/timeout failure (curl -f hides the body but still honours -w).
    cmd.extend(["-w", f"\n{_STATUS_MARK}%{{http_code}}", url])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout + 3,
        check=False,
    )
    out = proc.stdout.decode("utf-8", "replace")
    status = None
    idx = out.rfind("\n" + _STATUS_MARK)
    if idx != -1:
        tail = out[idx:].split(_STATUS_MARK, 1)[-1].strip()
        out = out[:idx]
        if tail.isdigit():
            status = int(tail)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip() or f"curl exit {proc.returncode}"
        if status:
            err = f"{err} HTTP {status}"
        raise RuntimeError(err)
    return out


def _urllib_get(url: str, timeout: int = TIMEOUT, headers: dict | None = None) -> str:
    hdrs = {**DEFAULT_HEADERS, "Accept-Encoding": "gzip", **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")


def _get(url: str, timeout: int = TIMEOUT, headers: dict | None = None,
         connect_timeout: int = 5) -> str:
    if CURL_BIN:
        try:
            return _curl_get(url, timeout=timeout, headers=headers,
                             connect_timeout=connect_timeout)
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

def _yahoo_pace() -> None:
    """Serialise Yahoo requests and keep them at least YAHOO_MIN_INTERVAL apart."""
    global _YAHOO_LAST_AT
    with _YAHOO_LOCK:
        wait = YAHOO_MIN_INTERVAL - (time.time() - _YAHOO_LAST_AT)
        if wait > 0:
            time.sleep(wait)
        _YAHOO_LAST_AT = time.time()


def _yahoo_session_cookie(force: bool = False) -> str | None:
    """Fetch a Yahoo session cookie once per hour — reduces HTTP 429s.

    fc.yahoo.com answers 404 but sets the cookies the chart API likes, so we
    only read the response headers. Best effort: failures are ignored.
    """
    global _YAHOO_COOKIE, _YAHOO_COOKIE_AT
    if not YAHOO_USE_COOKIE or not CURL_BIN:
        return None
    if _YAHOO_COOKIE and not force and (time.time() - _YAHOO_COOKIE_AT) < 3600:
        return _YAHOO_COOKIE
    try:
        proc = subprocess.run(
            [CURL_BIN, "-4", "-g", "-sS", "-D", "-", "-o", os.devnull,
             "--connect-timeout", "5", "--max-time", "8",
             "-A", BROWSER_UA, "https://fc.yahoo.com/"],
            capture_output=True, timeout=12, check=False,
        )
        cookies = []
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("set-cookie:"):
                cookies.append(line.split(":", 1)[1].split(";", 1)[0].strip())
        _YAHOO_COOKIE = "; ".join(dict.fromkeys(cookies)) or None
        _YAHOO_COOKIE_AT = time.time()
    except Exception:
        _YAHOO_COOKIE = None
    return _YAHOO_COOKIE


def _yahoo_headers() -> dict | None:
    cookie = _yahoo_session_cookie()
    return {"Cookie": cookie} if cookie else None


def _parse_chart_result(result) -> list[tuple[str, float]]:
    """[(date, close)] from a Yahoo v8 chart `result` object."""
    if not result:
        return []
    ts_list = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []
    out: dict[str, float] = {}
    for ts, c in zip(ts_list, closes):
        if ts is None or c is None:
            continue
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        if v > 0:
            d = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            out[d] = round(v, 4)
    return sorted(out.items())


def _yahoo_history(symbol: str, days: int = 150):
    """Fetch daily closes from Yahoo Finance (batch cache -> v8 chart API)."""
    ysym = YAHOO_MAP.get(symbol, symbol)

    cached = _spark_take(symbol)
    if cached:
        note_provider_ok("yahoo")
        return cached[-days:]

    if not provider_available("yahoo"):
        raise RuntimeError("yahoo: cooling down after recent 429/failures")

    enc = urllib.parse.quote(ysym, safe="")
    rng = "6mo" if days <= 130 else "1y"
    headers = _yahoo_headers()
    _yahoo_pace()

    last_err = None
    tried = 0
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        hkey = f"yahoo:{host}"
        if not provider_available(hkey):
            continue
        tried += 1
        url = (
            f"https://{host}/v8/finance/chart/{enc}"
            f"?range={rng}&interval=1d&includePrePost=false"
        )
        try:
            j = _get_json(url, timeout=10, headers=headers)
            out = _parse_chart_result(((j.get("chart") or {}).get("result") or [None])[0])
            if out:
                note_provider_ok("yahoo")
                note_provider_ok(hkey)
                return out[-days:]
            last_err = last_err or RuntimeError("empty response")
        except Exception as exc:
            last_err = exc
            unreachable, limited = _classify(exc)
            if limited:
                # 429 is per-IP: back off on the whole provider, skip host 2.
                note_provider_failure("yahoo", exc, rate_limited=True)
                break
            note_provider_failure(hkey, exc, unreachable=unreachable)
    if last_err is None:
        last_err = RuntimeError(f"all yahoo hosts cooling down ({tried} tried)")
    raise RuntimeError(f"yahoo({ysym}): {last_err}")


def _yahoo_spark_batch(symbols, days: int = 150) -> dict[str, list]:
    """ONE request for many symbols via the v7 spark endpoint.

    Returns {symbol: [(date, close)]}. Yahoo rate-limits per IP, so fetching the
    whole board in a single call is the difference between "works" and "429".
    """
    syms = list(dict.fromkeys(symbols))
    if not syms:
        return {}
    if not provider_available("yahoo"):
        raise RuntimeError("yahoo: cooling down after recent 429/failures")

    pairs = [(s, YAHOO_MAP.get(s, s)) for s in syms]
    ylist = ",".join(dict.fromkeys(y for _, y in pairs))
    rng = "6mo" if days <= 130 else "1y"
    url = (
        "https://query1.finance.yahoo.com/v7/finance/spark?symbols="
        f"{urllib.parse.quote(ylist, safe='')}&range={rng}&interval=1d"
    )
    _yahoo_pace()
    try:
        j = _get_json(url, timeout=15, headers=_yahoo_headers())
    except Exception as exc:
        unreachable, limited = _classify(exc)
        note_provider_failure("yahoo", exc, rate_limited=limited, unreachable=unreachable)
        raise RuntimeError(f"yahoo-spark: {exc}")

    by_yahoo: dict[str, list] = {}
    for item in ((j.get("spark") or {}).get("result") or []):
        ysym = item.get("symbol")
        resp = (item.get("response") or [None])[0]
        if not ysym or not resp:
            continue
        rows = _parse_chart_result(resp)
        if rows:
            by_yahoo[ysym] = rows

    out = {s: by_yahoo[y][-days:] for s, y in pairs if y in by_yahoo}
    if not out:
        note_provider_failure("yahoo", RuntimeError("spark returned no series"))
        raise RuntimeError(f"yahoo-spark: no data for {len(pairs)} symbol(s)")
    note_provider_ok("yahoo")
    return out


def prefetch_yahoo(symbols, days: int = 150) -> int:
    """Warm the shared spark cache with one batched Yahoo request (best effort).

    Called by app.py before a refresh cycle: 15 symbols then cost one HTTP call
    instead of fifteen. Returns how many symbols were cached.
    """
    syms = [s for s in dict.fromkeys(symbols) if s]
    if not syms:
        return 0
    try:
        got = _yahoo_spark_batch(syms, days=days)
    except Exception as exc:
        print(f"[sources] yahoo batch prefetch: {exc}", flush=True)
        return 0
    now = time.time()
    with _SPARK_LOCK:
        for sym, hist in got.items():
            _SPARK_CACHE[sym] = (now, hist)
    print(f"[sources] yahoo batch prefetch: {len(got)}/{len(syms)} symbols in one request",
          flush=True)
    return len(got)


def _spark_take(symbol: str, max_age: int = 900):
    """Consume a prefetched spark history if it is still fresh."""
    with _SPARK_LOCK:
        item = _SPARK_CACHE.pop(symbol, None)
    if not item:
        return None
    at, hist = item
    if not hist or (time.time() - at) > max_age:
        return None
    return hist


def clear_spark_cache() -> None:
    with _SPARK_LOCK:
        _SPARK_CACHE.clear()


def _probe_stooq_endpoint(scheme, domain, port_tag, probe_timeout):
    key = f"stooq:{domain}{port_tag}"
    if not provider_available(key):
        return
    d1 = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
    d2 = date.today().strftime("%Y%m%d")
    url = f"{scheme}://{domain}/q/d/l/?s=%5Espx&d1={d1}&d2={d2}&i=d"
    try:
        _get(url, timeout=probe_timeout, connect_timeout=probe_timeout)
        note_provider_ok(key)
    except Exception as exc:  # noqa: BLE001 — a probe must never break warming
        unreachable, limited = _classify(exc)
        note_provider_failure(key, exc, unreachable=unreachable, rate_limited=limited)
        print(f"[sources] {key} unreachable — skipping it for "
              f"{BREAKER_COOLDOWN // 60} min", flush=True)


def warm_providers(probe_timeout: int = 3) -> None:
    """Pre-flight reachability probe for the slow optional providers.

    stooq times out at TCP level from many data-centre IPs. Without this probe
    the first symbol of every refresh cycle pays that timeout once per host;
    one round of parallel 3 s probes trips the breakers so the whole fan-out
    skips those hosts for the next BREAKER_COOLDOWN seconds.
    """
    with ThreadPoolExecutor(max_workers=len(STOOQ_ENDPOINTS),
                            thread_name_prefix="probe") as pool:
        futures = [pool.submit(_probe_stooq_endpoint, scheme, domain, tag, probe_timeout)
                   for scheme, domain, tag in STOOQ_ENDPOINTS]
        for fut in futures:
            try:
                fut.result()
            except Exception:  # pragma: no cover — probe errors are already noted
                pass


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
    """Try Stooq .com / .pl CSV endpoints (https, plus http as a last resort).

    Each host gets its own circuit breaker: on the LXC, Cloudflare/egress issues
    make stooq time out at TCP level, and retrying it for all 15 symbols is what
    used to add ~45 s per card.
    """
    d1 = (date.today() - timedelta(days=int(days * 1.6))).strftime("%Y%m%d")
    d2 = date.today().strftime("%Y%m%d")
    candidates = STOOQ_ALIASES.get(symbol, [symbol])
    last_err = None
    tried = 0
    for cand in candidates:
        enc = urllib.parse.quote(cand, safe="")
        for scheme, domain, port_tag in STOOQ_ENDPOINTS:
            key = f"stooq:{domain}{port_tag}"
            if not provider_available(key):
                continue
            tried += 1
            url = f"{scheme}://{domain}/q/d/l/?s={enc}&d1={d1}&d2={d2}&i=d"
            try:
                text = _get(url, timeout=6, connect_timeout=3)
                hist = _parse_stooq_csv(text)
                if hist:
                    note_provider_ok(key)
                    return hist[-days:]
                last_err = last_err or RuntimeError("empty CSV")
            except Exception as exc:
                last_err = exc
                unreachable, limited = _classify(exc)
                note_provider_failure(key, exc, unreachable=unreachable, rate_limited=limited)
    if last_err is None:
        last_err = RuntimeError(f"all stooq hosts cooling down ({tried} tried)")
    raise RuntimeError(f"stooq({symbol}): {last_err}")


def _fred_quote_history(symbol: str, days: int = 150):
    """Fetch quote history from FRED's public CSV, gated on freshness.

    FRED keeps discontinued series (e.g. DCOILWTI stopped in 2020) alive but
    frozen, so a quote source must refuse data older than its `max_age_days`
    instead of showing a six-year-old price as if it were today's.
    """
    if symbol not in FRED_QUOTE_MAP:
        raise RuntimeError(f"fred: unmapped symbol {symbol}")
    sid, max_age = FRED_QUOTE_MAP[symbol]
    # Monthly IMF series need a long window for a usable sparkline.
    window = 20 if sid.endswith("USDM") else 2
    rows = _fred_csv_series(sid, window_years=window)
    valid = [(d, v) for d, v in rows if v is not None and v > 0]
    if not valid:
        raise RuntimeError(f"fred({sid}): no valid observations")
    last_date = valid[-1][0]
    try:
        age = (date.today() - date.fromisoformat(last_date)).days
    except ValueError:
        age = 0
    if age > max_age:
        raise RuntimeError(
            f"fred({sid}): last observation {last_date} is {age}d old (> {max_age}d)")
    return valid[-days:]


def _coingecko_history(symbol: str, days: int = 90):
    """Tokenised-metal fallback (PAXG for gold, KAG for silver)."""
    coin = COINGECKO_MAP.get(symbol)
    if not coin:
        raise RuntimeError(f"coingecko: unmapped symbol {symbol}")
    if not provider_available("coingecko"):
        raise RuntimeError("coingecko: cooling down after recent failures")
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart"
        f"?vs_currency=usd&days={min(days, 90)}&interval=daily"
    )
    try:
        j = _get_json(url, timeout=10)
    except Exception as exc:
        unreachable, limited = _classify(exc)
        note_provider_failure("coingecko", exc, unreachable=unreachable, rate_limited=limited)
        raise RuntimeError(f"coingecko({coin}): {exc}")
    dedup = {}
    for item in (j.get("prices") or []):
        if len(item) >= 2 and item[1]:
            d = datetime.fromtimestamp(item[0] / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            dedup[d] = round(float(item[1]), 2)
    out = sorted(dedup.items())
    if not out:
        note_provider_failure("coingecko", RuntimeError(f"{coin}: no data"))
        raise RuntimeError(f"coingecko({coin}): no data")
    note_provider_ok("coingecko")
    return out


def _coingecko_gold_history(days: int = 90):
    """Back-compat wrapper — gold via PAX Gold (1 PAXG = 1 troy oz)."""
    return _coingecko_history("xauusd", days=days)


def stooq_history(symbol: str, days: int = 150):
    """Multi-source market history: Yahoo -> FRED -> Stooq -> CoinGecko.

    Kept under the name `stooq_history` so app.py and demo.py share a single
    interface. Each provider is circuit-broken, so a dead upstream costs
    milliseconds instead of being retried for every symbol on the board.
    """
    errs = []
    providers = [("yahoo", lambda: _yahoo_history(symbol, days=days))]

    if symbol in FRED_QUOTE_MAP:
        sid, _max_age = FRED_QUOTE_MAP[symbol]
        providers.append((f"fred:{sid}", lambda: _fred_quote_history(symbol, days=days)))

    providers.append(("stooq", lambda: _stooq_raw_history(symbol, days=days)))

    if symbol in COINGECKO_MAP:
        providers.append((f"coingecko:{COINGECKO_MAP[symbol]}",
                          lambda: _coingecko_history(symbol, days=days)))

    for src_name, fn in providers:
        try:
            hist = fn()
            if hist:
                if src_name.startswith("fred:") and symbol in FRED_QUOTE_MAP:
                    max_age = FRED_QUOTE_MAP[symbol][1]
                elif src_name.startswith("coingecko:"):
                    max_age = 3
                else:
                    max_age = 14
                _LAST_SOURCE[symbol] = (src_name, max_age)
                return hist
            errs.append(f"{src_name}: empty")
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

def _fred_parse_csv(text: str):
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
    return out


def _fred_cutoff_rows(rows, years: int):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    return [x for x in rows if x[0] >= cutoff]


def _fred_csv_series(series_id: str, years: int | None = None,
                     window_years: int | None = None):
    """Fetch [(date, value|None)] from FRED's public fredgraph.csv with &cosd= window.

    `years`          -> cosd window AND a strict cutoff applied to the result
                        (used for rates / curve panels).
    `window_years`   -> cosd window only, no cutoff (used by quote lookups that
                        must be able to *see* that a series went stale).
    A 404 from fredgraph.csv usually means "no observations inside that window"
    (discontinued series), so we widen the window once before giving up — that
    turns a mystery 404 into an actionable "last observation 2020-04-24".
    """
    eff_window = window_years or years or 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * eff_window + 15)).strftime("%Y-%m-%d")
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={cutoff}"
    if not provider_available("fred"):
        # Skip straight to the official fallbacks instead of eating a 12s timeout
        # for every one of the ~13 FRED series on the board.
        raise RuntimeError("fred: cooling down after recent timeouts/429s")
    try:
        text = _get(url, timeout=12)
    except Exception as exc:
        unreachable, limited = _classify(exc)
        if unreachable or limited:
            note_provider_failure("fred", exc, unreachable=unreachable, rate_limited=limited)
        if "404" in str(exc) and eff_window < 20:
            text = _get(
                f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
                f"&cosd={(datetime.now(timezone.utc) - timedelta(days=365 * 20)).strftime('%Y-%m-%d')}",
                timeout=12,
            )
        else:
            raise
    else:
        note_provider_ok("fred")

    out = _fred_parse_csv(text)
    if window_years is None and years:
        out = _fred_cutoff_rows(out, years)
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
    errs = []
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
        except Exception as exc:
            errs.append(f"{yr}: {exc}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * eff_years)).strftime("%Y-%m-%d")
    series = [(d, v) for d, v in sorted(out.items()) if d >= cutoff]
    if not series:
        detail = f" ({'; '.join(errs)})" if errs else ""
        raise RuntimeError(f"ustreasury: no 10Y-2Y curve data{detail}")
    return series


def _ustreasury_breakeven_series(years: int | None = None):
    """Official US Treasury fallback for T10YIE (Nominal 10Y minus TIPS Real 10Y)."""
    cur_year = date.today().year
    nom_10y = {}
    real_10y = {}
    errs = []
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
        except Exception as exc:
            errs.append(f"{yr}: {exc}")
    eff_years = years or 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365 * eff_years)).strftime("%Y-%m-%d")
    out = [
        (d, round(nom_10y[d] - real_10y[d], 2))
        for d in sorted(set(nom_10y) & set(real_10y))
        if d >= cutoff
    ]
    if not out:
        detail = f" ({'; '.join(errs)})" if errs else ""
        raise RuntimeError(f"ustreasury: no 10Y breakeven data{detail}")
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
    # CLI diagnostics for the server:
    #   python3 /opt/econ/dashboard/sources.py            one check per panel
    #   python3 /opt/econ/dashboard/sources.py --doctor   exhaustive provider probe
    import sys

    def _run_checks(checks, width=18):
        ok = 0
        for label, fn in checks:
            t0 = time.time()
            try:
                res = fn()
                ok += 1
                print(f"  [OK]   {label:<{width}} ({time.time() - t0:5.2f}s) -> {res}")
            except Exception as exc:  # noqa: BLE001 — diagnostics must keep going
                print(f"  [FAIL] {label:<{width}} ({time.time() - t0:5.2f}s) -> {exc}")
        print(f"  -> {ok}/{len(checks)} passed")
        return ok

    def _fred_probe(sid, monthly=False):
        rows = _fred_csv_series(sid, window_years=20 if monthly else 2)
        valid = [(d, v) for d, v in rows if v is not None]
        if not valid:
            raise RuntimeError("no valid observations")
        d, v = valid[-1]
        age = (date.today() - date.fromisoformat(d)).days
        return f"last {d} = {v} ({age}d old, {len(valid)} obs)"

    def _stooq_probe(cand, scheme, domain, port_tag):
        key = f"stooq:{domain}{port_tag}"
        d1 = (date.today() - timedelta(days=180)).strftime("%Y%m%d")
        d2 = date.today().strftime("%Y%m%d")
        url = (f"{scheme}://{domain}/q/d/l/?s={urllib.parse.quote(cand, safe='')}"
               f"&d1={d1}&d2={d2}&i=d")
        rows = _parse_stooq_csv(_get(url, timeout=8))
        if not rows:
            raise RuntimeError("empty CSV")
        return f"{len(rows)} rows, last {rows[-1][0]} = {rows[-1][1]}"

    def _print_breakers():
        st = provider_status()
        if not st:
            print("  (no provider breakers tripped)")
            return
        for b in st:
            print(f"  {b['provider']}: {b['state']} retry_in={b['retry_in']}s "
                  f"fails={b['fails']} :: {b['reason']}")

    def self_test():
        print(f"curl binary: {CURL_BIN or 'not found (using urllib)'}")
        _run_checks([
            ("fx_latest", lambda: fx_latest(["EUR", "JPY", "GBP"])),
            ("worldbank_cpi", lambda: worldbank_indicator(["US", "EMU"], "FP.CPI.TOTL.ZG")),
            ("rate:DFF", lambda: fred_series("DFF", years=1)[-1]),
            ("rate:ECBDFR", lambda: fred_series("ECBDFR", years=1)[-1]),
            ("curve:T10Y2Y", lambda: fred_series("T10Y2Y", years=1)[-1]),
            ("breakeven:T10YIE", lambda: fred_series("T10YIE", years=1)[-1]),
            ("quote:^spx", lambda: stooq_history("^spx", days=30)[-1]),
            ("quote:cl.f", lambda: stooq_history("cl.f", days=30)[-1]),
            ("quote:xauusd", lambda: stooq_history("xauusd", days=30)[-1]),
        ])
        print("provider breakers:")
        _print_breakers()

    def doctor():
        reset_breakers()
        now = datetime.now(timezone.utc).astimezone()
        print(f"econ source doctor — {now.isoformat(timespec='seconds')}")
        print(f"curl: {CURL_BIN or 'NOT FOUND (urllib only)'} | yahoo pace "
              f"{YAHOO_MIN_INTERVAL}s | yahoo cookie {'on' if YAHOO_USE_COOKIE else 'off'}")

        print("\n[1/6] FX & macro")
        _run_checks([
            ("frankfurter.app", lambda: _get_json(
                "https://api.frankfurter.app/latest?from=USD&to=EUR,JPY")["date"]),
            ("frankfurter.dev", lambda: _get_json(
                "https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR,JPY")["date"]),
            ("worldbank CPI", lambda: worldbank_indicator(["US"], "FP.CPI.TOTL.ZG")[0]["value"]),
            ("worldbank GDP", lambda: worldbank_indicator(["US"], "NY.GDP.MKTP.KD.ZG")[0]["value"]),
        ], width=24)

        print("\n[2/6] Rates, curve & their official fallbacks")
        _run_checks([
            ("fred DFF", lambda: fred_series("DFF", years=1)[-1]),
            ("fred ECBDFR", lambda: fred_series("ECBDFR", years=1)[-1]),
            ("fred T10Y2Y", lambda: fred_series("T10Y2Y", years=1)[-1]),
            ("fred T10YIE", lambda: fred_series("T10YIE", years=1)[-1]),
            ("nyfed EFFR", lambda: _nyfed_effr_series(years=1)[-1]),
            ("ecb DFR", lambda: _ecb_dfr_series(years=1)[-1]),
            ("ustreasury 10y-2y", lambda: _ustreasury_curve_series(years=1)[-1]),
            ("ustreasury breakeven", lambda: _ustreasury_breakeven_series(years=1)[-1]),
        ], width=24)

        print("\n[3/6] FRED quote series (mapped to board cards)")
        _run_checks([
            (f"fred {sid}", lambda s=sid, m=sid.endswith("USDM"): _fred_probe(s, m))
            for _, (sid, _age) in sorted(FRED_QUOTE_MAP.items())
        ], width=24)

        print("\n[4/6] FRED candidate series (not wired in yet)")
        _run_checks([
            (f"fred {sid}", lambda s=sid: _fred_probe(s, monthly=s.endswith("USDM")))
            for sid in FRED_PROBE_CANDIDATES
        ], width=24)

        print("\n[5/6] Yahoo Finance")
        _run_checks([
            ("cookie handshake", lambda: _yahoo_session_cookie(force=True) or "no cookie set"),
            ("spark batch (all)", lambda: sorted(
                _yahoo_spark_batch(list(YAHOO_MAP), days=30).keys())),
            ("chart ^GSPC", lambda: _yahoo_history("^spx", days=30)[-1]),
            ("chart ^GDAXI", lambda: _yahoo_history("^dax", days=30)[-1]),
            ("chart ^HSI", lambda: _yahoo_history("^hsi", days=30)[-1]),
        ], width=24)

        print("\n[6/6] Stooq & CoinGecko")
        checks = []
        for scheme, domain, tag in (("https", "stooq.com", ""),
                                    ("https", "stooq.pl", ""),
                                    ("http", "stooq.com", ":80")):
            checks.append((f"{scheme}://{domain}",
                           lambda d=domain, s=scheme, t=tag: _stooq_probe("^dax", s, d, t)))
        for sym, coin in COINGECKO_MAP.items():
            checks.append((f"coingecko {coin}",
                           lambda c=coin, s=sym: _coingecko_history(s, days=30)[-1]))
        _run_checks(checks, width=24)

        print("\nProvider breakers after the probe:")
        _print_breakers()

    (doctor if "--doctor" in sys.argv else self_test)()
