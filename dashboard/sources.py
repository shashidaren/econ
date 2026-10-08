"""Data fetchers — free, key-less public endpoints with multi-source fallbacks.

Every function returns plain Python data and raises on failure; the cache
layer in app.py decides how to handle errors (serve stale data, show a badge).

Why multi-source + curl -4 transport?
  On Debian LXC containers, Python's stdlib urllib.request (HTTP/1.1 only,
  no Happy Eyeballs IPv4 fallback, OpenSSL default JA3) can get tarpitted by
  Akamai (fred.stlouisfed.org), blocked by Cloudflare (stooq.com), or 429'd
  by Yahoo Finance when using a custom bot User-Agent. Using curl -4 (HTTP/2,
  IPv4-forced, browser UA) plus independent fallback providers per panel keeps
  every section resilient. urllib is only a bounded fallback for TLS/HTTP-
  protocol errors; TCP failures and completed HTTP error statuses are not retried.

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

# FRED is healthy and quick from the deployed LXC. Keep a bounded per-request
# budget, and make the one wider retry cheaper than the normal fetch.
FRED_TIMEOUT = 8
FRED_WIDEN_TIMEOUT = 5
FRED_WIDEN_YEARS = 5
FRED_DEAD_TTL = 24 * 60 * 60

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
# Monthly IMF series publish with a lag, so 120 days is the operational limit.
FRED_QUOTE_MAP = {
    "^spx": ("SP500", 10),
    "^ndx": ("NASDAQ100", 10),
    "^dji": ("DJIA", 10),
    "^nkx": ("NIKKEI225", 10),
    "cl.f": ("POILWTIUSDM", 120),
    "cb.f": ("DCOILBRENTEU", 10),
    "hg.f": ("PCOPPUSDM", 120),
    "zw.f": ("PWHEAMTUSDM", 120),
    "al.f": ("PALUMUSDM", 120),
    "ni.f": ("PNICKUSDM", 120),
}

# Brent has a fresh daily FRED series today; this IMF monthly series is a
# fallback if the daily endpoint disappears, 404s, or goes stale.
FRED_QUOTE_FALLBACKS = {
    "cb.f": (("POILBREUSDM", 120),),
}

# These two optional panels are deliberately FRED-only: no unverified Yahoo or
# Stooq symbols are sent for aluminum/nickel.
FRED_ONLY_SYMBOLS = {"al.f", "ni.f"}

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

# CoinGecko tokenised-metal fallbacks (1 token ~= 1 troy oz). Both mappings
# were confirmed by the live LXC doctor run on 2026-10-08.
COINGECKO_MAP = {
    "xauusd": "pax-gold",
    "xagusd": "kinesis-silver",
}

# CNBC quote symbol mapping for equity indices (key-less public JSON webservice).
# Provides fallback quotes for indices that lack FRED coverage (Euro Stoxx, DAX,
# FTSE, Shanghai Composite, Hang Seng).
CNBC_MAP = {
    "^spx": ".SPX",
    "^ndx": ".NDX",
    "^dji": ".DJI",
    "^stx": ".STOXX50E",
    "^dax": ".GDAXI",
    "^ukx": ".FTSE",
    "^nkx": ".N225",
    "^shc": ".SSEC",
    "^hsi": ".HSI",
}


# Additional FRED series to probe before wiring them into the board. All
# confirmed series are now in FRED_QUOTE_MAP; the 404s PSILVUSDM and
# GOLDAMGBD228NLBM have been removed.
FRED_PROBE_CANDIDATES = []

# Tracks which provider last served a given symbol -> shown on the card footer
_LAST_SOURCE: dict[str, tuple[str, int | None]] = {}
_YAHOO_LOCK = threading.Lock()
_YAHOO_LAST_AT = 0.0
_YAHOO_COOKIE: str | None = None
_YAHOO_COOKIE_AT = 0.0
_YAHOO_CRUMB: str | None = None
_YAHOO_CRUMB_AT = 0.0
_SPARK_CACHE: dict[str, tuple[float, list]] = {}
_SPARK_LOCK = threading.Lock()
_CNBC_CACHE: dict[str, tuple[float, list[tuple[str, float]]]] = {}
_CNBC_LOCK = threading.Lock()


# 404s for series IDs are stable across refreshes. Cache confirmed dead IDs for
# one day so every market refresh does not pay for the same missing series.
_FRED_DEAD: dict[str, float] = {}
_FRED_DEAD_LOCK = threading.Lock()


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


def reset_breakers(provider: str | None = None) -> None:
    """Reset all breakers, or only one provider and its host-specific keys."""
    with _BREAKER_LOCK:
        if provider is None:
            _BREAKERS.clear()
            return
        prefix = f"{provider}:"
        for name in list(_BREAKERS):
            if name == provider or name.startswith(prefix):
                _BREAKERS.pop(name, None)


def _fred_is_dead(series_id: str) -> bool:
    now = time.monotonic()
    with _FRED_DEAD_LOCK:
        expires = _FRED_DEAD.get(series_id)
        if expires is None:
            return False
        if expires <= now:
            _FRED_DEAD.pop(series_id, None)
            return False
        return True


def _fred_mark_dead(series_id: str) -> None:
    with _FRED_DEAD_LOCK:
        _FRED_DEAD[series_id] = time.monotonic() + FRED_DEAD_TTL


def reset_fred_dead_cache() -> None:
    """Clear the process-local negative cache (mainly useful to tests/doctor)."""
    with _FRED_DEAD_LOCK:
        _FRED_DEAD.clear()


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
        # curl itself has --max-time above; one extra second lets it exit cleanly
        # without giving the urllib fallback another full independent timeout.
        timeout=timeout + 1,
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


def _curl_error_allows_urllib(exc) -> bool:
    """Only retry curl failures that look TLS/HTTP-protocol specific.

    A connect timeout or refusal is not improved by opening another client:
    urllib would repeat the same TCP failure, often across every DNS address.
    HTTP status responses (including 404/429) are completed HTTP exchanges, not
    protocol failures, and must be passed through without a second request.
    """
    message = str(exc).lower()
    if any(token in message for token in (
        "failed to connect", "couldn't connect", "connection refused",
        "connection timeout", "connection timed out", "timeout was reached",
        "operation timed out", "no route to host", "could not resolve host",
        "curl: (6)", "curl: (7)", "curl: (28)",
    )):
        return False
    # Do not retry a completed HTTP response such as HTTP 404 or HTTP 429.
    if any(
        f"http {status}" in message or f"http error {status}" in message
        for status in range(100, 600)
    ):
        return False
    return any(token in message for token in (
        "ssl", "tls", "http/2", "http2", "http protocol", "protocol error",
        "stream error", "curl: (16)", "curl: (35)", "curl: (92)",
    ))


def _get(url: str, timeout: int = TIMEOUT, headers: dict | None = None,
         connect_timeout: int = 5) -> str:
    if not CURL_BIN:
        return _urllib_get(url, timeout=timeout, headers=headers)

    started = time.monotonic()
    deadline = started + max(1.0, float(timeout) + 1.0)
    try:
        return _curl_get(url, timeout=timeout, headers=headers,
                         connect_timeout=connect_timeout)
    except Exception as curl_exc:
        if not _curl_error_allows_urllib(curl_exc):
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise curl_exc
        try:
            # Share a single wall-clock budget: the fallback never gets a fresh
            # full `timeout` after curl has already spent time on the request.
            return _urllib_get(url, timeout=remaining, headers=headers)
        except Exception:
            raise curl_exc


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


def _fetch_yahoo_crumb(cookie: str) -> tuple[str | None, str | None]:
    """Try query1 and query2 to fetch a Yahoo crumb using the session cookie.

    Returns (crumb, error_message).
    """
    if not CURL_BIN or not cookie:
        return None, "curl not available or no cookie"
    last_err = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        cmd = [
            CURL_BIN, "-4", "-g", "-fsSL",
            "--connect-timeout", "5", "--max-time", "8",
            "-A", BROWSER_UA,
            "-H", f"Cookie: {cookie}",
            "-w", f"\n{_STATUS_MARK}%{{http_code}}",
            f"https://{host}/v1/test/getcrumb",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=12, check=False)
            out = proc.stdout.decode("utf-8", "replace")
            status = None
            idx = out.rfind("\n" + _STATUS_MARK)
            if idx != -1:
                tail = out[idx:].split(_STATUS_MARK, 1)[-1].strip()
                out = out[:idx]
                if tail.isdigit():
                    status = int(tail)
            if proc.returncode == 0:
                crumb = out.strip()
                if crumb and len(crumb) < 60 and not crumb.startswith("<") and "{" not in crumb:
                    return crumb, None
                last_err = f"{host}: invalid crumb body ({crumb[:25]})"
            else:
                err = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
                if status:
                    err = f"{err} HTTP {status}"
                last_err = f"{host}: {err}"
        except Exception as exc:
            last_err = f"{host}: {exc}"
    return None, last_err


def _yahoo_session_crumb(force: bool = False) -> str | None:
    """Fetch a Yahoo session crumb using the session cookie.

    Yahoo query APIs require a crumb matching the session cookie to prevent
    HTTP 429/401 errors. Cached for 1 hour alongside the cookie.
    """
    global _YAHOO_CRUMB, _YAHOO_CRUMB_AT
    if not YAHOO_USE_COOKIE or not CURL_BIN:
        return None
    if _YAHOO_CRUMB and not force and (time.time() - _YAHOO_CRUMB_AT) < 3600:
        return _YAHOO_CRUMB
    cookie = _yahoo_session_cookie(force=force)
    if not cookie:
        _YAHOO_CRUMB = None
        return None
    crumb, _ = _fetch_yahoo_crumb(cookie)
    if crumb:
        _YAHOO_CRUMB = crumb
        _YAHOO_CRUMB_AT = time.time()
    else:
        _YAHOO_CRUMB = None
    return _YAHOO_CRUMB



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
    crumb = _yahoo_session_crumb()
    crumb_param = f"&crumb={urllib.parse.quote(crumb, safe='')}" if crumb else ""

    last_err = None
    tried = 0
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        hkey = f"yahoo:{host}"
        if not provider_available(hkey):
            continue
        tried += 1
        url = (
            f"https://{host}/v8/finance/chart/{enc}"
            f"?range={rng}&interval=1d&includePrePost=false{crumb_param}"
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
    crumb = _yahoo_session_crumb()
    crumb_param = f"&crumb={urllib.parse.quote(crumb, safe='')}" if crumb else ""
    url = (
        "https://query1.finance.yahoo.com/v7/finance/spark?symbols="
        f"{urllib.parse.quote(ylist, safe='')}&range={rng}&interval=1d{crumb_param}"
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
    # FRED-only cards do not have verified Yahoo tickers; keep them out of the
    # batch rather than asking Yahoo for internal dashboard aliases.
    syms = [s for s in dict.fromkeys(symbols) if s and s in YAHOO_MAP]
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
    """Fetch a freshness-gated FRED quote, trying declared fallbacks in order."""
    if symbol not in FRED_QUOTE_MAP:
        raise RuntimeError(f"fred: unmapped symbol {symbol}")

    candidates = (FRED_QUOTE_MAP[symbol],) + FRED_QUOTE_FALLBACKS.get(symbol, ())
    errs = []
    for sid, max_age in candidates:
        # Five years gives monthly IMF series enough history for a useful chart,
        # while the primary daily series only needs a short freshness window.
        window = FRED_WIDEN_YEARS if sid.endswith("USDM") else 2
        try:
            rows = _fred_csv_series(sid, window_years=window)
            valid = [(d, v) for d, v in rows if v is not None and v > 0]
            if not valid:
                raise RuntimeError("no valid observations")
            last_date = valid[-1][0]
            try:
                age = (date.today() - date.fromisoformat(last_date)).days
            except ValueError:
                age = 0
            if age > max_age:
                raise RuntimeError(
                    f"last observation {last_date} is {age}d old (> {max_age}d)")
            # stooq_history() uses this to report the actual primary/fallback
            # series and the correct daily-vs-monthly staleness threshold.
            _LAST_SOURCE[symbol] = (f"fred:{sid}", max_age)
            return valid[-days:]
        except Exception as exc:
            errs.append(f"{sid}: {exc}")
    raise RuntimeError(f"fred({symbol}): all mapped series failed -> {' | '.join(errs)}")


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


def _parse_cnbc_quotes(data) -> list[dict]:
    """Extract list of QuickQuote dicts from CNBC JSON response."""
    if not isinstance(data, dict):
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []
    qq_res = data.get("QuickQuoteResult") or {}
    quotes = qq_res.get("QuickQuote")
    if quotes is None:
        eq_res = data.get("ExtendedQuoteResult") or {}
        eq_items = eq_res.get("ExtendedQuote") or []
        if isinstance(eq_items, dict):
            eq_items = [eq_items]
        quotes = [item.get("QuickQuote") for item in eq_items
                  if isinstance(item, dict) and isinstance(item.get("QuickQuote"), dict)]
    if isinstance(quotes, dict):
        quotes = [quotes]
    return [q for q in (quotes or []) if isinstance(q, dict)]


def _match_cnbc_quote_symbol(q: dict) -> str:
    sym = str(q.get("symbol") or q.get("altSymbol") or q.get("providerSymbol") or "").strip()
    return sym.split(":", 1)[0]


def _cnbc_quote_to_history(q: dict) -> list[tuple[str, float]]:
    last_str = q.get("last")
    if not last_str:
        return []
    try:
        last_val = float(str(last_str).replace(",", ""))
    except (TypeError, ValueError):
        return []
    if last_val <= 0:
        return []

    prev_val = None
    prev_str = q.get("previous_day_closing")
    if prev_str:
        try:
            prev_val = float(str(prev_str).replace(",", ""))
        except (TypeError, ValueError):
            pass
    if prev_val is None:
        chg_str = q.get("change")
        if chg_str:
            try:
                chg_val = float(str(chg_str).replace(",", ""))
                prev_val = last_val - chg_val
            except (TypeError, ValueError):
                pass
    if prev_val is None or prev_val <= 0:
        prev_val = last_val

    last_time = str(q.get("last_time") or q.get("reg_last_time") or "")
    dt = date.today()
    if len(last_time) >= 10 and last_time[:4].isdigit():
        try:
            dt = date.fromisoformat(last_time[:10])
        except ValueError:
            pass

    d_today = dt.isoformat()
    d_prev = (dt - timedelta(days=3 if dt.weekday() == 0 else 1)).isoformat()
    return [(d_prev, round(prev_val, 4)), (d_today, round(last_val, 4))]


def clear_cnbc_cache() -> None:
    with _CNBC_LOCK:
        _CNBC_CACHE.clear()


def _cnbc_batch(symbols: list[str]) -> dict[str, list[tuple[str, float]]]:
    """Fetch quotes for multiple symbols from CNBC in ONE request."""
    if not provider_available("cnbc"):
        raise RuntimeError("cnbc: cooling down after recent failures")

    pairs = [(s, CNBC_MAP[s]) for s in symbols if s in CNBC_MAP]
    if not pairs:
        return {}

    csyms = "|".join(dict.fromkeys(c for _, c in pairs))
    url = (
        "https://quote.cnbc.com/quote-html-webservice/quote.htm"
        f"?noform=1&partnerId=2&fund=1&exthrs=0&output=json&symbolType=issue"
        f"&symbols={urllib.parse.quote(csyms, safe='')}&requestMethod=quick"
    )
    try:
        j = _get_json(url, timeout=10)
        quotes = _parse_cnbc_quotes(j)
        by_csym = {}
        for q in quotes:
            cs = _match_cnbc_quote_symbol(q)
            hist = _cnbc_quote_to_history(q)
            if cs and hist:
                by_csym[cs] = hist

        out = {s: by_csym[c] for s, c in pairs if c in by_csym}
        if out:
            note_provider_ok("cnbc")
            now = time.time()
            with _CNBC_LOCK:
                for s, h in out.items():
                    _CNBC_CACHE[s] = (now, h)
            return out
        raise RuntimeError(f"cnbc: no quote returned for {csyms}")
    except Exception as exc:
        unreachable, limited = _classify(exc)
        note_provider_failure("cnbc", exc, unreachable=unreachable, rate_limited=limited)
        raise RuntimeError(f"cnbc-batch: {exc}")


def _cnbc_history(symbol: str, days: int = 150):
    """Fetch recent quote history for a symbol via CNBC (cached batch -> single)."""
    if symbol not in CNBC_MAP:
        raise RuntimeError(f"no cnbc mapping for {symbol}")

    now = time.time()
    with _CNBC_LOCK:
        item = _CNBC_CACHE.get(symbol)
        if item and (now - item[0]) < 600:
            return item[1]

    if not provider_available("cnbc"):
        raise RuntimeError("cnbc: cooling down after recent failures")

    # Request all missing CNBC-mapped symbols together so 5 cards cost 1 HTTP request
    missing = [s for s in CNBC_MAP if s not in _CNBC_CACHE or (now - _CNBC_CACHE[s][0]) >= 600]
    got = _cnbc_batch(missing or [symbol])
    if symbol in got:
        return got[symbol]
    raise RuntimeError(f"cnbc({CNBC_MAP[symbol]}): no quote in response")


def stooq_history(symbol: str, days: int = 150):
    """Multi-source market history: Yahoo -> FRED -> CNBC -> Stooq -> CoinGecko.

    Kept under the name `stooq_history` so app.py and demo.py share a single
    interface. Each provider is circuit-broken, so a dead upstream costs
    milliseconds instead of being retried for every symbol on the board.
    """
    errs = []
    providers = []
    if symbol not in FRED_ONLY_SYMBOLS:
        providers.append(("yahoo", lambda: _yahoo_history(symbol, days=days)))

    if symbol in FRED_QUOTE_MAP:
        providers.append(("fred", lambda: _fred_quote_history(symbol, days=days)))

    if symbol in CNBC_MAP:
        providers.append(("cnbc", lambda: _cnbc_history(symbol, days=days)))

    if symbol not in FRED_ONLY_SYMBOLS:
        providers.append(("stooq", lambda: _stooq_raw_history(symbol, days=days)))

    if symbol in COINGECKO_MAP:
        providers.append((f"coingecko:{COINGECKO_MAP[symbol]}",
                          lambda: _coingecko_history(symbol, days=days)))

    for src_name, fn in providers:
        try:
            hist = fn()
            if hist:
                if src_name == "fred":
                    src_name, max_age = _LAST_SOURCE.get(
                        symbol, (f"fred:{FRED_QUOTE_MAP[symbol][0]}",
                                 FRED_QUOTE_MAP[symbol][1]))
                elif src_name.startswith("coingecko:"):
                    max_age = 3
                elif src_name == "cnbc":
                    src_name = f"cnbc:{CNBC_MAP[symbol]}"
                    max_age = 5
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
    """Fetch FRED CSV data using a bounded window and a short 404 retry.

    `years`          -> cosd window AND a strict cutoff applied to the result
                        (used for rates / curve panels).
    `window_years`   -> cosd window only, no cutoff (used by quote lookups that
                        must be able to *see* whether a series has gone stale).
    A 404 is retried once with at most FRED_WIDEN_YEARS of history. A second 404
    confirms the ID is unavailable in the useful window, so it is negatively
    cached for FRED_DEAD_TTL rather than retried on every 10-minute refresh.
    """
    if _fred_is_dead(series_id):
        raise RuntimeError(
            f"fred({series_id}): negative-cached after HTTP 404 for "
            f"{FRED_DEAD_TTL // 3600}h")

    eff_window = max(1, int(window_years or years or 1))

    def url_for(window):
        cutoff = (datetime.now(timezone.utc) -
                  timedelta(days=365 * window + 15)).strftime("%Y-%m-%d")
        return ("https://fred.stlouisfed.org/graph/fredgraph.csv"
                f"?id={series_id}&cosd={cutoff}")

    if not provider_available("fred"):
        raise RuntimeError("fred: cooling down after recent timeouts/429s")

    try:
        text = _get(url_for(eff_window), timeout=FRED_TIMEOUT)
    except Exception as exc:
        unreachable, limited = _classify(exc)
        if unreachable or limited:
            note_provider_failure("fred", exc, unreachable=unreachable, rate_limited=limited)
        if not _is_http_404(exc):
            raise

        # Do not make the retry window unbounded. If this request already
        # covered the maximum useful span, this 404 alone confirms the ID is
        # dead for this dashboard's purposes.
        if eff_window >= FRED_WIDEN_YEARS:
            _fred_mark_dead(series_id)
            raise RuntimeError(
                f"fred({series_id}): HTTP 404 within {eff_window}y window; "
                f"negative-cached for {FRED_DEAD_TTL // 3600}h") from exc

        try:
            text = _get(url_for(FRED_WIDEN_YEARS), timeout=FRED_WIDEN_TIMEOUT)
        except Exception as widen_exc:
            if _is_http_404(widen_exc):
                _fred_mark_dead(series_id)
                raise RuntimeError(
                    f"fred({series_id}): HTTP 404 in {eff_window}y and "
                    f"{FRED_WIDEN_YEARS}y windows; negative-cached for "
                    f"{FRED_DEAD_TTL // 3600}h") from widen_exc
            raise RuntimeError(
                f"fred({series_id}): initial HTTP 404 at {eff_window}y; "
                f"{FRED_WIDEN_YEARS}y retry failed: {widen_exc}") from widen_exc
        else:
            note_provider_ok("fred")
    else:
        note_provider_ok("fred")

    out = _fred_parse_csv(text)
    if window_years is None and years:
        out = _fred_cutoff_rows(out, years)
    if not any(v is not None for _, v in out):
        raise RuntimeError(f"fred: no data for {series_id}")
    return out


def _is_http_404(exc) -> bool:
    message = str(exc).lower()
    return ("http 404" in message or "http error 404" in message or
            ("curl: (22)" in message and "404" in message))


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


def _treasury_row_value(row: dict, header: str):
    """Read a Treasury CSV column without depending on header capitalization."""
    wanted = " ".join(header.casefold().split())
    for name, value in row.items():
        if " ".join(str(name).strip().casefold().split()) == wanted:
            return value
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
                d = _parse_us_date(_treasury_row_value(row, "Date") or "")
                y10 = _treasury_row_value(row, "10 Yr")
                y2 = _treasury_row_value(row, "2 Yr")
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
                d = _parse_us_date(_treasury_row_value(row, "Date") or "")
                v = _treasury_row_value(row, "10 Yr")
                if d and v:
                    nom_10y[d] = float(v)
            for row in _ustreasury_csv_year(yr, "daily_treasury_real_yield_curve"):
                d = _parse_us_date(_treasury_row_value(row, "Date") or "")
                v = _treasury_row_value(row, "10 Yr")
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


def _doctor_yahoo_cookie():
    """Validate the cookie handshake without printing the cookie value."""
    if not _yahoo_session_cookie(force=True):
        raise RuntimeError("no cookie set")
    return "cookie set"


def _doctor_yahoo_crumb():
    """Probe the crumb endpoint using the session cookie."""
    cookie = _yahoo_session_cookie()
    if not cookie:
        raise RuntimeError("cannot probe crumb without cookie")
    crumb, err = _fetch_yahoo_crumb(cookie)
    if not crumb:
        raise RuntimeError(err or "failed to obtain crumb")
    return f"crumb set ({len(crumb)} chars)"


def _doctor_yahoo(run_checks):
    """Probe cookie, crumb, and v8 charts independently before the spark batch.

    A spark 429 must not trip the shared breaker first and turn every chart
    check into a misleading zero-second "cooling down" result. Each chart probe
    clears only Yahoo's breakers; the batch gets its own clean test afterward.
    """
    run_checks([
        ("cookie handshake", _doctor_yahoo_cookie),
        ("crumb handshake", _doctor_yahoo_crumb),
    ], width=24)

    for label, symbol in (("chart ^GSPC", "^spx"),
                          ("chart ^GDAXI", "^dax"),
                          ("chart ^HSI", "^hsi")):
        reset_breakers("yahoo")
        run_checks([(label, lambda s=symbol: _yahoo_history(s, days=30)[-1])], width=24)

    reset_breakers("yahoo")
    run_checks([
        ("spark batch (all)", lambda: sorted(
            _yahoo_spark_batch(list(YAHOO_MAP), days=30).keys())),
    ], width=24)


def _doctor_cnbc(run_checks):
    """Probe CNBC quote endpoints independently for international indices."""
    for label, sym in (("cnbc .GDAXI (DAX)", "^dax"),
                       ("cnbc .FTSE (UK)", "^ukx"),
                       ("cnbc .HSI (Hang Seng)", "^hsi"),
                       ("cnbc .SSEC (Shanghai)", "^shc"),
                       ("cnbc .STOXX50E (Euro Stoxx)", "^stx")):
        reset_breakers("cnbc")
        clear_cnbc_cache()
        def _probe(s=sym):
            h = _cnbc_history(s, days=30)
            return f"last {h[-1][0]} = {h[-1][1]}"
        run_checks([(label, _probe)], width=28)

    reset_breakers("cnbc")
    clear_cnbc_cache()
    run_checks([
        ("cnbc batch (all 5)", lambda: f"{len(_cnbc_batch(['^dax', '^ukx', '^hsi', '^shc', '^stx']))}/5 indices"),
    ], width=28)



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

        print("\n[1/7] FX & macro")
        _run_checks([
            ("frankfurter.app", lambda: _get_json(
                "https://api.frankfurter.app/latest?from=USD&to=EUR,JPY")["date"]),
            ("frankfurter.dev", lambda: _get_json(
                "https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR,JPY")["date"]),
            ("worldbank CPI", lambda: worldbank_indicator(["US"], "FP.CPI.TOTL.ZG")[0]["value"]),
            ("worldbank GDP", lambda: worldbank_indicator(["US"], "NY.GDP.MKTP.KD.ZG")[0]["value"]),
        ], width=24)

        print("\n[2/7] Rates, curve & their official fallbacks")
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

        print("\n[3/7] FRED quote series (mapped to board cards, including fallbacks)")
        mapped_fred = {sid for sid, _age in FRED_QUOTE_MAP.values()}
        mapped_fred.update(
            sid for fallbacks in FRED_QUOTE_FALLBACKS.values()
            for sid, _age in fallbacks
        )
        _run_checks([
            (f"fred {sid}", lambda s=sid, m=sid.endswith("USDM"): _fred_probe(s, m))
            for sid in sorted(mapped_fred)
        ], width=24)

        print("\n[4/7] FRED candidate series (not wired in yet)")
        if FRED_PROBE_CANDIDATES:
            _run_checks([
                (f"fred {sid}", lambda s=sid: _fred_probe(s, monthly=s.endswith("USDM")))
                for sid in FRED_PROBE_CANDIDATES
            ], width=24)
        else:
            print("  (no unverified candidates)")

        print("\n[5/7] Yahoo Finance (cookie + crumb handshake, charts, spark batch)")
        _doctor_yahoo(_run_checks)

        print("\n[6/7] CNBC quote candidates (international indices)")
        _doctor_cnbc(_run_checks)

        print("\n[7/7] Stooq & CoinGecko")
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
