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
                    CNBC / Tencent-Sina indices -> Stooq -> CoinGecko metals
  * US stocks       Sina US daily K -> Nasdaq historical -> Tencent K-line
                    (per-host breakers) -> Yahoo spark batch -> Stooq .us;
                    WAF/HTML block pages are detected from the response body
                    and cool only the blocked host (see _stock_host_get)
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
import math
import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


class HttpStatusError(RuntimeError):
    """An HTTP error status whose response body was captured, not swallowed.

    Raised only on the `capture_error_body=True` transport path (US stock
    history + doctor probes). curl's default `-f` discards error bodies, which
    is how Tencent's 2026-10-21 WAF 501 page hid behind a bare "curl: (22)"
    log line; callers here get the `status` and `body` so a WAF block page can
    be told apart from an ordinary HTTP error.
    """

    def __init__(self, status: int, body: str = "", message: str | None = None):
        self.status = status
        self.body = body or ""
        super().__init__(message or f"HTTP {status}")


def _curl_get(url: str, timeout: int = TIMEOUT, headers: dict | None = None,
              connect_timeout: int = 5, capture_error_body: bool = False) -> str:
    hdrs = {**DEFAULT_HEADERS, **(headers or {})}
    cmd = [
        CURL_BIN,
        "-4",                       # Force IPv4 (avoids broken IPv6 routing in LXC)
        "-g",                       # Disable URL globbing for ^, [], {}
        # Default: fail on HTTP >=400. With capture_error_body the -f is dropped
        # so the error body survives for WAF detection (see HttpStatusError).
        "-sSL" if capture_error_body else "-fsSL",
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
    if capture_error_body and status is not None and status >= 400:
        # curl exited 0 because -f was dropped; surface the error body so the
        # caller can recognise a WAF block page instead of parsing it as data.
        raise HttpStatusError(status, out)
    return out


def _urllib_get(url: str, timeout: int = TIMEOUT, headers: dict | None = None,
                capture_error_body: bool = False) -> str:
    hdrs = {**DEFAULT_HEADERS, "Accept-Encoding": "gzip", **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if not capture_error_body:
            raise
        try:
            raw = exc.read() or b""
            if (exc.headers.get("Content-Encoding") or "").lower() == "gzip":
                raw = gzip.decompress(raw)
            body = raw.decode("utf-8", "replace")
        except Exception:
            body = ""
        raise HttpStatusError(exc.code, body) from exc


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
         connect_timeout: int = 5, capture_error_body: bool = False) -> str:
    if not CURL_BIN:
        return _urllib_get(url, timeout=timeout, headers=headers,
                           capture_error_body=capture_error_body)

    started = time.monotonic()
    deadline = started + max(1.0, float(timeout) + 1.0)
    try:
        return _curl_get(url, timeout=timeout, headers=headers,
                         connect_timeout=connect_timeout,
                         capture_error_body=capture_error_body)
    except Exception as curl_exc:
        # A captured HTTP error body is a completed HTTP exchange (and the body
        # is the whole point); never re-ask urllib and lose it.
        if isinstance(curl_exc, HttpStatusError):
            raise
        if not _curl_error_allows_urllib(curl_exc):
            raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise curl_exc
        try:
            # Share a single wall-clock budget: the fallback never gets a fresh
            # full `timeout` after curl has already spent time on the request.
            return _urllib_get(url, timeout=remaining, headers=headers,
                               capture_error_body=capture_error_body)
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



def _parse_chart_result_with_volume(result) -> list[tuple[str, float, float | None]]:
    """[(date, close, volume)] from a Yahoo chart/spark result object."""
    if not result:
        return []
    ts_list = result.get("timestamp") or []
    quotes = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []
    volumes = quotes.get("volume") or []
    out: dict[str, tuple[float, float | None]] = {}
    for i, (ts, c) in enumerate(zip(ts_list, closes)):
        if ts is None or c is None:
            continue
        try:
            close = float(c)
            if close <= 0 or not math.isfinite(close):
                continue
            raw_volume = volumes[i] if i < len(volumes) else None
            volume = float(raw_volume) if raw_volume is not None else None
            if volume is not None and (volume < 0 or not math.isfinite(volume)):
                volume = None
            d = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            out[d] = (round(close, 4), volume)
        except (TypeError, ValueError, OverflowError):
            continue
    return [(d, close, volume) for d, (close, volume) in sorted(out.items())]


def _parse_chart_result(result) -> list[tuple[str, float]]:
    """[(date, close)] from a Yahoo v8 chart `result` object."""
    return [(d, close) for d, close, _volume in _parse_chart_result_with_volume(result)]


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


def _yahoo_spark_batch(symbols, days: int = 150, *, include_volume: bool = False) -> dict[str, list]:
    """ONE request for many symbols via the v7 spark endpoint.

    Returns {symbol: [(date, close)]}, or [(date, close, volume)] for the stock
    screener. Yahoo rate-limits per IP, so fetching a watchlist in one call is
    preferable to one request per ticker.
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
        rows = (_parse_chart_result_with_volume(resp) if include_volume
                else _parse_chart_result(resp))
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


def _parse_stooq_csv_with_volume(text: str):
    """Yield (date_str, close, volume) for a Stooq stock-history CSV."""
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            d = (row.get("Date") or "").strip()
            close = float(row["Close"])
        except (KeyError, TypeError, ValueError):
            continue
        if not d or close <= 0 or not math.isfinite(close):
            continue
        try:
            volume = float(row.get("Volume") or "")
            if volume < 0 or not math.isfinite(volume):
                volume = None
        except (TypeError, ValueError):
            volume = None
        out.append((d, close, volume))
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


# Tencent / Sina key-less quotes for the five indices FRED does not publish.
# 2026-10-08 probe: Tencent covers Shanghai, Hang Seng, and FTSE; Sina covers all
# five. Sina int_dax / int_ftse / int_fsx5e are not used (empty or a stale FTSE).
ASIA_QUOTE_MAP = {
    "^shc": {"tencent": "sh000001", "sina": "s_sh000001"},
    "^hsi": {"tencent": "hkHSI", "sina": "int_hangseng"},
    "^ukx": {"tencent": "ukUKX", "sina": "b_UKX"},
    "^dax": {"tencent": None, "sina": "b_DAX"},
    "^stx": {"tencent": None, "sina": "b_SX5E"},
}
_ASIA_CACHE: dict[str, tuple[float, list[tuple[str, float]], str]] = {}
_ASIA_LOCK = threading.Lock()
_ASIA_TTL = 600


def clear_asia_cache() -> None:
    with _ASIA_LOCK:
        _ASIA_CACHE.clear()


def _iso_date(raw: str) -> str | None:
    raw = (raw or "").strip()
    if len(raw) >= 8 and raw[:8].isdigit() and raw[4:6] in {f"{m:02d}" for m in range(1, 13)}:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    raw = raw.replace("/", "-")
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return None


def _num(raw) -> float | None:
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_tencent_body(text: str) -> dict[str, list[tuple[str, float]]]:
    """code -> [(prev_date, prev), (date, last)] from a qt.gtimg.cn body."""
    out = {}
    for chunk in (text or "").split(";"):
        if '="' not in chunk:
            continue
        left, _, right = chunk.partition('="')
        code = left.strip().split("v_", 1)[-1]
        fields = right.rstrip('"').split("~")
        if code == "pv_none_match" or len(fields) < 5:
            continue
        last, prev = _num(fields[3]), _num(fields[4])
        if last is None:
            continue
        dates = [d for d in (_iso_date(f) for f in fields) if d]
        day = max(dates) if dates else date.today().isoformat()
        prev_day = day
        if prev is None:
            prev = last
        out[code] = [(prev_day, prev), (day, last)]
    return out


def _parse_sina_body(text: str) -> dict[str, list[tuple[str, float]]]:
    """Sina symbol -> two-point history. Ignores empty var bodies."""
    out = {}
    for chunk in (text or "").split(";"):
        if '="' not in chunk:
            continue
        left, _, right = chunk.partition('="')
        code = left.strip().split("hq_str_", 1)[-1]
        fields = right.rstrip('"').split(",")
        if len(fields) < 3 or not fields[1]:
            continue
        last = _num(fields[1])
        if last is None:
            continue
        chg = _num(fields[2])
        prev = last - chg if chg is not None else last
        dates = [d for d in (_iso_date(f) for f in fields) if d]
        day = max(dates) if dates else date.today().isoformat()
        out[code] = [(day, prev), (day, last)]
    return out


def _asia_batch(symbols) -> dict[str, list[tuple[str, float]]]:
    """One Tencent call plus one Sina call. Tencent wins when both answer."""
    wanted = [s for s in symbols if s in ASIA_QUOTE_MAP]
    if not wanted:
        return {}
    tencent_codes = [ASIA_QUOTE_MAP[s]["tencent"] for s in wanted if ASIA_QUOTE_MAP[s]["tencent"]]
    sina_codes = [ASIA_QUOTE_MAP[s]["sina"] for s in wanted]
    headers = {"Referer": "https://finance.sina.com.cn/"}
    tencent_hits, sina_hits = {}, {}
    errors = []
    if tencent_codes and provider_available("tencent"):
        url = "https://qt.gtimg.cn/q=" + ",".join(tencent_codes)
        try:
            tencent_hits = _parse_tencent_body(_get(url, timeout=8, headers={"Referer": "https://gu.qq.com/"}))
            if tencent_hits:
                note_provider_ok("tencent")
            else:
                raise RuntimeError("tencent: empty")
        except Exception as exc:
            errors.append(str(exc))
            note_provider_failure("tencent", exc)
    if sina_codes and provider_available("sina"):
        url = "https://hq.sinajs.cn/list=" + ",".join(sina_codes)
        try:
            sina_hits = _parse_sina_body(_get(url, timeout=8, headers=headers))
            if sina_hits:
                note_provider_ok("sina")
            else:
                raise RuntimeError("sina: empty")
        except Exception as exc:
            errors.append(str(exc))
            note_provider_failure("sina", exc)
    now = time.time()
    got = {}
    with _ASIA_LOCK:
        for sym in wanted:
            spec = ASIA_QUOTE_MAP[sym]
            hist = None
            label = None
            if spec["tencent"] and spec["tencent"] in tencent_hits:
                hist = tencent_hits[spec["tencent"]]
                label = f"tencent:{spec['tencent']}"
            elif spec["sina"] in sina_hits:
                hist = sina_hits[spec["sina"]]
                label = f"sina:{spec['sina']}"
            if hist:
                _ASIA_CACHE[sym] = (now, hist, label)
                got[sym] = hist
    if not got and errors:
        raise RuntimeError(" | ".join(errors))
    return got


def _asia_history(symbol: str, days: int = 150):
    """Cached Tencent-then-Sina quote for an index FRED does not publish."""
    if symbol not in ASIA_QUOTE_MAP:
        raise RuntimeError(f"no asia mapping for {symbol}")
    now = time.time()
    with _ASIA_LOCK:
        item = _ASIA_CACHE.get(symbol)
        if item and (now - item[0]) < _ASIA_TTL:
            _LAST_SOURCE[symbol] = (item[2], 5)
            return item[1]
    missing = [s for s in ASIA_QUOTE_MAP
               if s not in _ASIA_CACHE or (now - _ASIA_CACHE[s][0]) >= _ASIA_TTL]
    _asia_batch(missing or [symbol])
    with _ASIA_LOCK:
        item = _ASIA_CACHE.get(symbol)
    if not item:
        raise RuntimeError(f"asia({symbol}): no quote")
    _LAST_SOURCE[symbol] = (item[2], 5)
    return item[1]


def stooq_history(symbol: str, days: int = 150):
    """Multi-source market history: Yahoo -> FRED -> CNBC -> Tencent/Sina -> Stooq -> CoinGecko.

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

    if symbol in ASIA_QUOTE_MAP:
        providers.append(("asia", lambda: _asia_history(symbol, days=days)))

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
                elif src_name == "asia":
                    src_name, max_age = _LAST_SOURCE.get(symbol, ("asia", 5))
                else:
                    max_age = 14
                _LAST_SOURCE[symbol] = (src_name, max_age)
                return hist
            errs.append(f"{src_name}: empty")
        except Exception as exc:
            errs.append(f"{src_name}: {exc}")

    raise RuntimeError(f"all sources failed -> {' | '.join(errs)}")


# --------------------------------------------------------------------------
# US stock daily history — Sina -> Nasdaq -> Tencent -> Yahoo -> Stooq
#
# Rebuilt 2026-10-21 after Tencent's K-line WAF started answering HTTP 501
# (an HTML page bouncing to waf.tencent.com) on BOTH K-line hosts from the
# LXC's IP, while curl -f swallowed the body and a single shared
# "tencent_kline" breaker took the whole cascade down with it. The rules now:
#   * every vendor HOST has its own breaker key — a block on one provider or
#     host must never disable another;
#   * error bodies are captured (capture_error_body) so a WAF/HTML block page
#     is recognised, cools only its own host for 30 min, and is reported as
#     "blocked by WAF" in the per-ticker diagnostics;
#   * requests per host are paced with jitter and each refresh has a request
#     budget, so a 32-symbol sweep cannot trip a WAF by bursting;
#   * every fetcher returns the same [(date, close, volume)] row contract, so
#     stocks.py (7-day window, risk buckets, filters) is untouched.
# The bare "sina" breaker already belongs to the hq.sinajs.cn index-quote
# host, so the daily-K host gets a host-scoped key under the "sina" family.
# --------------------------------------------------------------------------

# Minimum gap between two requests to the same stock-history host (seconds),
# plus up to 50% jitter. Yahoo has its own slower pacing (_yahoo_pace).
STOCK_PACE_SECONDS = float(os.environ.get("ECON_STOCK_PACE", "0.3"))
# Hard cap on HTTP requests inside one stock_history_batch() refresh. Two
# full 32-symbol per-symbol sweeps fit with room to spare; a pathological
# every-host-for-every-symbol storm does not.
STOCK_REFRESH_REQUEST_BUDGET = int(os.environ.get("ECON_STOCK_REQUEST_BUDGET", "128"))

SINA_KLINE_HOST = "stock.finance.sina.com.cn"
SINA_KLINE_KEY = f"sina:{SINA_KLINE_HOST}"
NASDAQ_HOST = "api.nasdaq.com"
NASDAQ_KEY = f"nasdaq:{NASDAQ_HOST}"
# (host, fq-kline base URL). The proxy host serves the same backend under a
# different hostname/WAF, so each gets its own breaker: tencent_kline:<host>.
TENCENT_KLINE_HOSTS = (
    ("web.ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"),
    ("proxy.finance.qq.com", "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get"),
)

SINA_KLINE_HEADERS = {"Referer": "https://finance.sina.com.cn/"}
NASDAQ_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.nasdaq.com/",
}
TENCENT_KLINE_HEADERS = {"Referer": "https://gu.qq.com/"}

_HOST_PACE_LOCK = threading.Lock()
_HOST_LAST_AT: dict[str, float] = {}


def _pace_host(key: str, interval: float | None = None) -> None:
    """Serialise requests to one vendor host with a small jittered gap.

    Same shape as _yahoo_pace but keyed per host breaker, so the Sina sweep
    and the Nasdaq sweep never slow each other down. A non-positive interval
    disables pacing (tests).
    """
    if interval is None:
        interval = STOCK_PACE_SECONDS
    if interval <= 0:
        return
    with _HOST_PACE_LOCK:
        wait = _HOST_LAST_AT.get(key, 0.0) + interval - time.time()
        if wait > 0:
            time.sleep(wait + random.uniform(0.0, interval * 0.5))
        _HOST_LAST_AT[key] = time.time()


class _RequestBudget:
    """Thread-safe cap on HTTP requests for one stock-history refresh."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True


class _SymbolMiss(RuntimeError):
    """The host answered but has no usable data for this symbol.

    A mapping/empty-result miss is not evidence the shared host is down, so it
    must never trip the host breaker; the cascade just tries the next provider
    for this one ticker.
    """


def _is_waf_block_body(text) -> bool:
    """True when a body is a WAF/HTML block page rather than market data."""
    if not text:
        return False
    if "waf.tencent.com" in text:
        return True
    head = text.lstrip(" \t\r\n\ufeff")[:64].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def _stock_host_get(url: str, key: str, *, headers: dict | None = None,
                    timeout: int = 10, connect_timeout: int = 5,
                    budget: _RequestBudget | None = None,
                    symbol_miss_statuses=(404,)) -> str:
    """One paced, budgeted GET against a stock-history host, with WAF detection.

    Returns the body on HTTP success. Transport failures and non-WAF HTTP
    errors note the *host* breaker via _classify (429 -> rate-limit cooldown,
    unreachable -> 30 min cooldown, otherwise two-strike). A WAF/HTML block
    page — whether it arrives as an HTTP error body or a 200 challenge page —
    cools only this host for BREAKER_COOLDOWN (30 min) and raises with
    "blocked by WAF" in the message so per-ticker diagnostics can say so.
    HTTP statuses in `symbol_miss_statuses` raise _SymbolMiss without
    touching the breaker.
    """
    if not provider_available(key):
        raise RuntimeError(f"{key} cooling down")
    _pace_host(key)
    if budget is not None and not budget.take():
        raise RuntimeError("stock refresh request budget exhausted")
    try:
        text = _get(url, timeout=timeout, headers=headers,
                    connect_timeout=connect_timeout, capture_error_body=True)
    except HttpStatusError as exc:
        if _is_waf_block_body(exc.body):
            msg = f"blocked by WAF (HTTP {exc.status})"
            note_provider_failure(key, msg, cooldown=BREAKER_COOLDOWN)
            raise RuntimeError(msg)
        if exc.status in symbol_miss_statuses:
            raise _SymbolMiss(f"HTTP {exc.status}")
        unreachable, limited = _classify(exc)
        note_provider_failure(key, exc, unreachable=unreachable, rate_limited=limited)
        raise RuntimeError(str(exc))
    except Exception as exc:
        unreachable, limited = _classify(exc)
        note_provider_failure(key, exc, unreachable=unreachable, rate_limited=limited)
        raise
    if _is_waf_block_body(text):
        msg = "blocked by WAF (HTML block page)"
        note_provider_failure(key, msg, cooldown=BREAKER_COOLDOWN)
        raise RuntimeError(msg)
    return text


def _sina_daily_k_url(symbol: str) -> str:
    """Sina US daily-K JSONP endpoint (full history; no count parameter)."""
    sym = urllib.parse.quote(str(symbol).upper(), safe="")
    return ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/"
            f"var%20s=/US_MinKService.getDailyK?symbol={sym}&___qn=3")


def _parse_sina_daily_k_payload(payload):
    """Parse Sina's `var s=([{d,o,h,l,c,v}, ...])` JSONP to sorted rows."""
    if isinstance(payload, str):
        text = payload.strip()
        # Strip the JSONP assignment wrapper (`var s=(` ... `);`, `var s=[...];`
        # or a bare array) by taking the outermost [ ... ] span.
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return []
        try:
            payload = json.loads(text[start:end + 1])
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, list):
        return []
    out = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        day = str(row.get("d") or "")[:10]
        try:
            date.fromisoformat(day)
            close = float(str(row.get("c")).replace(",", "").replace("$", ""))
        except (TypeError, ValueError):
            continue
        if close <= 0 or not math.isfinite(close):
            continue
        volume = None
        try:
            volume = float(str(row.get("v")).replace(",", ""))
            if volume < 0 or not math.isfinite(volume):
                volume = None
        except (TypeError, ValueError):
            volume = None
        out[day] = (day, close, volume)
    return [out[d] for d in sorted(out)]


def _sina_us_daily_k_history(symbol: str, days: int = 130, budget: _RequestBudget | None = None):
    """Sina US daily K-lines for one ticker (primary stock-history source)."""
    key = SINA_KLINE_KEY
    try:
        text = _stock_host_get(_sina_daily_k_url(symbol), key,
                               headers=SINA_KLINE_HEADERS, timeout=12, budget=budget)
        rows = _parse_sina_daily_k_payload(text)
        if not rows:
            raise _SymbolMiss(f"no daily K-lines for {symbol}")
        note_provider_ok(key)
        return rows[-days:]
    except Exception as exc:
        raise RuntimeError(f"sina-dailyk({symbol}): {exc}") from exc


def _nasdaq_historical_url(symbol: str, days: int = 130) -> str:
    """Nasdaq historical-quote API URL spanning enough days for `days` sessions."""
    todate = date.today()
    # ~1.7 calendar days per session plus slack clears weekends/holidays, so
    # days=130 asks for ~235 calendar days (~160 sessions) under a 200 limit.
    fromdate = todate - timedelta(days=int(int(days) * 1.7) + 14)
    limit = max(200, int(days) + 20)
    sym = urllib.parse.quote(str(symbol).upper(), safe="")
    return (f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass=stocks"
            f"&fromdate={fromdate.isoformat()}&todate={todate.isoformat()}&limit={limit}")


def _parse_nasdaq_historical_payload(payload):
    """Parse Nasdaq `data.tradesTable.rows` (newest first) to ascending rows."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    table = data.get("tradesTable") or {}
    raw_rows = table.get("rows") or []

    def _clean(raw):
        try:
            return float(str(raw).replace("$", "").replace(",", "").strip())
        except (TypeError, ValueError):
            return None

    out = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        raw_day = str(row.get("date") or "").strip()
        try:
            day = datetime.strptime(raw_day, "%m/%d/%Y").date().isoformat()
        except ValueError:
            try:
                day = date.fromisoformat(raw_day[:10]).isoformat()
            except ValueError:
                continue
        close = _clean(row.get("close"))
        if close is None or close <= 0 or not math.isfinite(close):
            continue
        volume = _clean(row.get("volume"))
        if volume is not None and (volume < 0 or not math.isfinite(volume)):
            volume = None
        out[day] = (day, close, volume)
    return [out[d] for d in sorted(out)]


def _nasdaq_us_stock_history(symbol: str, days: int = 130, budget: _RequestBudget | None = None):
    """Nasdaq historical daily bars for one ticker (needs the browser UA)."""
    key = NASDAQ_KEY
    try:
        text = _stock_host_get(_nasdaq_historical_url(symbol, days=days), key,
                               headers=NASDAQ_HEADERS, timeout=12, budget=budget)
        rows = _parse_nasdaq_historical_payload(text)
        if not rows:
            raise _SymbolMiss(f"no historical rows for {symbol}")
        note_provider_ok(key)
        return rows[-days:]
    except Exception as exc:
        raise RuntimeError(f"nasdaq({symbol}): {exc}") from exc


def _parse_tencent_kline_payload(payload, code: str):
    """Parse Tencent daily K-lines to [(date, close, volume)]."""
    if isinstance(payload, str):
        text = payload.strip()
        if "(" in text and not text.startswith("{"):
            text = text[text.find("(") + 1:text.rfind(")")].strip().rstrip(";")
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return []
    if not isinstance(payload, dict):
        return []
    data = payload.get("data") or {}
    item = data.get(code)
    if item is None:
        # Tencent occasionally normalizes the casing of a symbol key.
        item = next((v for k, v in data.items() if str(k).lower() == code.lower()), None)
    if not isinstance(item, dict):
        return []
    raw_rows = (item.get("qfqday") or item.get("day") or item.get("dayqfq") or [])
    out = {}
    for row in raw_rows:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        day = str(row[0])[:10]
        try:
            date.fromisoformat(day)
            close = float(str(row[2]).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if close <= 0 or not math.isfinite(close):
            continue
        volume = None
        if len(row) > 5:
            try:
                volume = float(str(row[5]).replace(",", ""))
                if volume < 0 or not math.isfinite(volume):
                    volume = None
            except (TypeError, ValueError):
                volume = None
        out[day] = (day, close, volume)
    return [out[d] for d in sorted(out)]


def _tencent_kline_url(base: str, code: str, days: int = 130) -> str:
    """Tencent fq-kline URL (commas percent-encoded; the WAF blocks both forms)."""
    params = urllib.parse.urlencode({"param": f"{code},day,,,{int(days)},qfq"})
    return f"{base}?{params}"


def _tencent_us_stock_history(code: str, days: int = 130, budget: _RequestBudget | None = None):
    """Fetch Tencent's adjusted daily K-lines for one US ticker, per host.

    Best effort only since 2026-10-21: both K-line hosts answer the LXC with a
    WAF 501 page. Each host has its own breaker (tencent_kline:<host>), so a
    block on web.ifzq.gtimg.cn neither disables proxy.finance.qq.com nor —
    as the old single "tencent_kline" key did — the whole cascade. A symbol
    miss (empty payload / HTTP 404) stops the host loop: both hosts front the
    same backend, so the second would miss too.
    """
    last_err = None
    for host, base in TENCENT_KLINE_HOSTS:
        key = f"tencent_kline:{host}"
        try:
            text = _stock_host_get(_tencent_kline_url(base, code, days=days), key,
                                   headers=TENCENT_KLINE_HEADERS, timeout=10,
                                   budget=budget)
            rows = _parse_tencent_kline_payload(text, code)
            if not rows:
                # Some Tencent responses include a callback wrapper; the parser
                # also accepts that form, so an empty result is a symbol miss.
                raise _SymbolMiss(f"no daily K-lines for {code}")
            note_provider_ok(key)
            return rows[-days:]
        except _SymbolMiss as exc:
            last_err = exc
            break
        except Exception as exc:
            last_err = exc
    if last_err is None:
        last_err = RuntimeError("all Tencent K-line hosts are cooling down")
    raise RuntimeError(f"tencent-kline({code}): {last_err}")


def _stooq_us_stock_history(symbol: str, days: int = 130, budget: _RequestBudget | None = None):
    """Stooq fallback for US shares, whose symbols use the `.us` suffix."""
    d1 = (date.today() - timedelta(days=int(days * 1.7))).strftime("%Y%m%d")
    d2 = date.today().strftime("%Y%m%d")
    last_err = None
    for scheme, domain, port_tag in STOOQ_ENDPOINTS:
        key = f"stooq:{domain}{port_tag}"
        if not provider_available(key):
            continue
        _pace_host(key)
        if budget is not None and not budget.take():
            last_err = RuntimeError("stock refresh request budget exhausted")
            break
        sym = urllib.parse.quote(f"{symbol.lower()}.us", safe="")
        url = f"{scheme}://{domain}/q/d/l/?s={sym}&d1={d1}&d2={d2}&i=d"
        try:
            text = _get(url, timeout=6, connect_timeout=3)
            rows = _parse_stooq_csv_with_volume(text)
            if rows:
                note_provider_ok(key)
                return rows[-days:]
            last_err = RuntimeError("empty stock CSV")
        except Exception as exc:
            last_err = exc
            unreachable, limited = _classify(exc)
            note_provider_failure(key, exc, unreachable=unreachable, rate_limited=limited)
    if last_err is None:
        last_err = RuntimeError("all Stooq hosts are cooling down")
    raise RuntimeError(f"stooq({symbol}.us): {last_err}")


def _stock_history_has_liquidity(rows):
    return (len(rows) >= 61
            and sum(1 for row in rows[-20:] if len(row) > 2 and row[2] is not None) >= 15)


def stock_history_batch(symbols, vendor_symbols=None, days: int = 130):
    """Fetch stock daily histories: Sina -> Nasdaq -> Tencent -> Yahoo -> Stooq.

    Sina's US daily-K and Nasdaq's historical API are the providers verified
    live from the LXC on 2026-10-21; Tencent K-line (WAF-blocked there) is
    best effort, Yahoo is one batched spark request, Stooq the last resort.
    Per-symbol stages run at most 4 concurrent fetches, each host is paced
    with jitter, and one request budget caps the whole refresh. Only
    histories with enough rows and usable volume fields are accepted for the
    liquidity screen. `_meta` records which cascade stages ran
    (`history_providers`, as breaker *family* names), the requests spent, and
    per-ticker errors for unresolved symbols.
    """
    symbols = list(dict.fromkeys(str(s).upper() for s in symbols if s))
    vendor_symbols = vendor_symbols or {}
    out = {}
    errors = []
    symbol_errors = {}
    budget = _RequestBudget(STOCK_REFRESH_REQUEST_BUDGET)
    # Provider families this refresh actually needed or touched (one per
    # cascade stage that ran). Downstream diagnostics scope to this list so
    # shared-breaker trouble from unrelated board features (index quotes,
    # spark charts) cannot mark a fully covered refresh degraded.
    history_providers = ["sina"]

    def add_symbol_error(symbol, provider, exc):
        symbol_errors.setdefault(symbol, []).append(f"{provider}: {exc}")

    def run_stage(missing, fetch_one, label, prefix):
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix=prefix) as pool:
            futures = {pool.submit(fetch_one, symbol): symbol for symbol in missing}
            for future, symbol in futures.items():
                try:
                    rows = future.result()
                    if not _stock_history_has_liquidity(rows):
                        raise RuntimeError(
                            f"{label} history/volume too short for {symbol}")
                    out[symbol] = {"rows": rows, "source": label}
                except Exception as exc:
                    errors.append(f"{symbol}: {exc}")
                    add_symbol_error(symbol, label, exc)

    run_stage(symbols,
              lambda s: _sina_us_daily_k_history(s, days=days, budget=budget),
              "Sina daily K", "stocks-sina")

    missing = [symbol for symbol in symbols if symbol not in out]
    if missing:
        history_providers.append("nasdaq")
        run_stage(missing,
                  lambda s: _nasdaq_us_stock_history(s, days=days, budget=budget),
                  "Nasdaq", "stocks-nasdaq")

    missing = [symbol for symbol in symbols if symbol not in out]
    if missing:
        history_providers.append("tencent_kline")

        def fetch_tencent(symbol):
            code = vendor_symbols.get(symbol)
            if not code:
                raise RuntimeError(f"no Tencent K-line mapping for {symbol}")
            return _tencent_us_stock_history(code, days=days, budget=budget)

        run_stage(missing, fetch_tencent, "Tencent K-line", "stocks-tencent")

    missing = [symbol for symbol in symbols if symbol not in out]
    if missing:
        # The Yahoo stage is relevant to this refresh even when its breaker is
        # already cooling and the attempt is skipped.
        history_providers.append("yahoo")
    if missing and provider_available("yahoo") and budget.take():
        try:
            yahoo_rows = _yahoo_spark_batch(missing, days=days, include_volume=True)
            for symbol in missing:
                rows = yahoo_rows.get(symbol)
                if rows and _stock_history_has_liquidity(rows):
                    out[symbol] = {"rows": rows, "source": "Yahoo Finance"}
                else:
                    add_symbol_error(
                        symbol, "Yahoo Finance",
                        "no usable history or volume returned",
                    )
        except Exception as exc:
            errors.append(f"Yahoo Finance batch: {exc}")
            for symbol in missing:
                add_symbol_error(symbol, "Yahoo Finance batch", exc)
    elif missing:
        reason = ("request budget exhausted" if provider_available("yahoo")
                  else "provider cooling down")
        for symbol in missing:
            add_symbol_error(symbol, "Yahoo Finance", reason)

    missing = [symbol for symbol in symbols if symbol not in out]
    if missing:
        history_providers.append("stooq")
        run_stage(missing,
                  lambda s: _stooq_us_stock_history(s, days=days, budget=budget),
                  "Stooq", "stocks-stooq")

    unavailable = [symbol for symbol in symbols if symbol not in out]
    if not out:
        detail = " | ".join(
            f"{symbol}: {'; '.join(symbol_errors.get(symbol, [])) or 'no usable history'}"
            for symbol in unavailable[:6]
        ) or " | ".join(errors[:6]) or "all providers unavailable"
        raise RuntimeError(f"no usable US stock histories ({detail})")
    meta = out.setdefault("_meta", {})
    meta["history_providers"] = list(history_providers)
    meta["history_requests"] = budget.used
    if unavailable:
        meta["history_errors"] = {
            "count": len(unavailable),
            "samples": {
                symbol: "; ".join(symbol_errors.get(symbol, [])) or
                "no usable history after provider cascade"
                for symbol in unavailable[:6]
            },
        }
    return out


# --------------------------------------------------------------------------
# US stock delayed / intraday quotes — Sina -> Nasdaq -> Tencent -> Yahoo
#
# Keyless ~15-min-delayed quote overlay for the displayed watchlist picks.
# Refreshed on the 5-minute dashboard cycle without touching the 1-hour daily
# screen's filters, momentum metrics, or ranking. Dedicated `stock_quote:*`
# breaker keys prevent quote hiccups from disabling index or history breakers.
# --------------------------------------------------------------------------

SINA_US_QUOTE_KEY = "stock_quote:sina"
NASDAQ_US_QUOTE_KEY = "stock_quote:nasdaq"
TENCENT_US_QUOTE_KEY = "stock_quote:tencent"
STOCK_QUOTE_REQUEST_BUDGET = 32
_CST_TZ = timezone(timedelta(hours=8))
_ET_TZ = ZoneInfo("America/New_York")


def us_market_session(now: datetime | None = None) -> dict:
    """U.S. Eastern equity session window (clock-based; exchange holidays excluded)."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    eastern = moment.astimezone(_ET_TZ)
    tz_abbr = eastern.strftime("%Z") or "ET"
    if eastern.weekday() >= 5:
        return {
            "state": "closed",
            "is_open": False,
            "label": f"U.S. market closed (weekend · {tz_abbr})",
        }
    minutes = eastern.hour * 60 + eastern.minute
    if 4 * 60 <= minutes < 9 * 60 + 30:
        return {
            "state": "pre_market",
            "is_open": False,
            "label": f"U.S. pre-market (04:00–09:30 {tz_abbr})",
        }
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return {
            "state": "regular",
            "is_open": True,
            "label": f"U.S. regular session (09:30–16:00 {tz_abbr})",
        }
    if 16 * 60 <= minutes < 20 * 60:
        return {
            "state": "after_hours",
            "is_open": False,
            "label": f"U.S. after-hours (16:00–20:00 {tz_abbr})",
        }
    return {
        "state": "closed",
        "is_open": False,
        "label": f"U.S. market closed (overnight · {tz_abbr})",
    }


def _finite_float(raw) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.upper() in ("UNCH", "UNCHANGED"):
        return 0.0
    cleaned = text.replace("$", "").replace("%", "").replace(",", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _pos_float(raw) -> float | None:
    value = _finite_float(raw)
    return value if (value is not None and value > 0) else None


def _parse_cst_timestamp(raw: str) -> tuple[str | None, str | None]:
    """Parse Sina's UTC+8 (Beijing/MYT) quote timestamp into UTC ISO."""
    text = (raw or "").strip()
    if not text:
        return None, None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=_CST_TZ)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds"), None
        except ValueError:
            continue
    try:
        day = date.fromisoformat(text[:10]).isoformat()
        return None, f"{day} (UTC+8)"
    except ValueError:
        return None, text[:48]


_ET_DATE_TIME_RE = re.compile(
    r"([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})"
    r"(?:.*?\b(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)))?",
    re.IGNORECASE,
)
_ET_TIME_FIRST_RE = re.compile(
    r"(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)).*?"
    r"([A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})",
    re.IGNORECASE,
)


def _parse_et_timestamp(raw: str) -> tuple[str | None, str | None]:
    """Parse U.S. Eastern quote timestamps (Nasdaq prose or ISO-like strings)."""
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    if not text:
        return None, None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text[:19], fmt).replace(tzinfo=_ET_TZ)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds"), None
        except ValueError:
            continue

    date_part, time_part = None, None
    m_first = _ET_TIME_FIRST_RE.search(text)
    if m_first:
        time_part, date_part = m_first.group(1), m_first.group(2)
    else:
        m_dt = _ET_DATE_TIME_RE.search(text)
        if m_dt:
            date_part, time_part = m_dt.group(1), m_dt.group(2)

    if date_part:
        norm_date = re.sub(r",\s*", ", ", date_part.strip())
        parsed_day = None
        for dfmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                parsed_day = datetime.strptime(norm_date, dfmt).date()
                break
            except ValueError:
                continue
        if parsed_day is not None:
            if time_part:
                norm_time = re.sub(r"\s+", " ", time_part.strip().upper())
                if " " not in norm_time and norm_time.endswith(("AM", "PM")):
                    norm_time = norm_time[:-2] + " " + norm_time[-2:]
                for tfmt in ("%I:%M:%S %p", "%I:%M %p"):
                    try:
                        tm = datetime.strptime(norm_time, tfmt).time()
                        dt = datetime.combine(parsed_day, tm, tzinfo=_ET_TZ)
                        return dt.astimezone(timezone.utc).isoformat(timespec="seconds"), None
                    except ValueError:
                        continue
            return None, f"{parsed_day.isoformat()} (U.S. Eastern)"
    return None, text[:64]


def _sina_us_code(symbol: str) -> str:
    clean = str(symbol or "").strip().lower().replace(".", "$").replace("-", "$")
    return f"gb_{clean}"


def _sina_us_quotes_url(symbols) -> str:
    codes = [_sina_us_code(s) for s in symbols if str(s or "").strip()]
    return "https://hq.sinajs.cn/list=" + ",".join(dict.fromkeys(codes))


def _parse_sina_us_quotes_payload(
    text: str,
    code_to_symbol: dict[str, str] | None = None,
    *,
    session_state: str = "regular",
) -> dict[str, dict]:
    """Parse Sina `var hq_str_gb_<sym>="..."` lines into normalized quote dicts."""
    out: dict[str, dict] = {}
    for chunk in (text or "").split(";"):
        if '="' not in chunk:
            continue
        left, _, right = chunk.partition('="')
        code = left.strip().split("hq_str_", 1)[-1].strip()
        if not code.startswith("gb_"):
            continue
        symbol = (code_to_symbol or {}).get(code)
        if not symbol:
            symbol = code[3:].replace("$", ".").upper()
        body = right.split('"', 1)[0]
        fields = body.split(",")
        if len(fields) < 5:
            continue
        price = _pos_float(fields[1])
        if price is None:
            continue
        change_pct = _finite_float(fields[2])
        change = _finite_float(fields[4])
        p26 = _pos_float(fields[26]) if len(fields) > 26 else None
        p_implied = (round(price - change, 4)
                     if (change is not None and price - change > 0) else None)
        if p26 is not None and (
            p_implied is None or abs(p26 - p_implied) <= max(0.05, price * 0.01)
        ):
            prev_close = round(p26, 4)
        else:
            prev_close = p_implied
        if change is None and prev_close:
            change = round(price - prev_close, 4)
        if change_pct is None and prev_close:
            change_pct = round((price / prev_close - 1.0) * 100.0, 2)

        open_p = _pos_float(fields[5]) if len(fields) > 5 else None
        day_high = _pos_float(fields[6]) if len(fields) > 6 else None
        day_low = _pos_float(fields[7]) if len(fields) > 7 else None
        if day_low is not None and day_high is not None and day_low > day_high:
            day_low, day_high = day_high, day_low
        vol_raw = _finite_float(fields[10]) if len(fields) > 10 else None
        volume = vol_raw if (vol_raw is not None and vol_raw >= 0) else None
        as_of_utc, as_of_display = _parse_cst_timestamp(fields[3])

        ext_price = None
        ext_pct = None
        ext_label = None
        if session_state != "regular" and len(fields) > 24:
            cand_ext = _pos_float(fields[21])
            ext_stamp = fields[24].strip()
            if cand_ext is not None and ext_stamp:
                ext_price = round(cand_ext, 4)
                ext_pct = _finite_float(fields[22])
                upper_stamp = ext_stamp.upper()
                if "AM" in upper_stamp:
                    ext_label = "Pre-market"
                elif "PM" in upper_stamp:
                    ext_label = "After-hours"
                else:
                    ext_label = "Extended hours"

        out[symbol] = {
            "symbol": symbol,
            "price": round(price, 4),
            "prev_close": prev_close,
            "change": round(change, 4) if change is not None else None,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "open": round(open_p, 4) if open_p is not None else None,
            "day_high": round(day_high, 4) if day_high is not None else None,
            "day_low": round(day_low, 4) if day_low is not None else None,
            "volume": volume,
            "extended_price": ext_price,
            "extended_change_pct": (round(ext_pct, 2)
                                    if ext_pct is not None else None),
            "extended_label": ext_label,
            "as_of_utc": as_of_utc,
            "as_of_display": as_of_display,
            "delayed": True,
            "source": "Sina US quote",
            "market_status": session_state,
        }
    return out


def _sina_us_quotes_batch(
    symbols,
    *,
    session_state: str = "regular",
    budget: _RequestBudget | None = None,
) -> dict[str, dict]:
    """Fetch delayed/intraday US quotes for multiple tickers in ONE Sina call."""
    wanted = [str(s).strip().upper() for s in symbols if str(s or "").strip()]
    if not wanted:
        return {}
    code_map = {_sina_us_code(s): s for s in wanted}
    url = _sina_us_quotes_url(wanted)
    text = _stock_host_get(
        url, SINA_US_QUOTE_KEY, headers=SINA_KLINE_HEADERS,
        timeout=8, budget=budget,
    )
    parsed = _parse_sina_us_quotes_payload(
        text, code_map, session_state=session_state,
    )
    if parsed:
        note_provider_ok(SINA_US_QUOTE_KEY)
        return parsed
    raise _SymbolMiss("Sina returned no usable US stock quotes")


def _nasdaq_quote_url(symbol: str) -> str:
    sym = urllib.parse.quote(str(symbol).strip().upper(), safe="")
    return f"https://api.nasdaq.com/api/quote/{sym}/info?assetclass=stocks"


def _map_nasdaq_market_status(raw: str | None, fallback: str) -> str:
    norm = re.sub(r"[^a-z]+", " ", (raw or "").lower()).strip()
    if not norm:
        return fallback
    if "pre" in norm:
        return "pre_market"
    if "after" in norm:
        return "after_hours"
    if "open" in norm:
        return "regular"
    if "close" in norm:
        return "closed"
    return fallback


def _parse_nasdaq_quote_payload(
    payload,
    symbol: str | None = None,
    *,
    session_state: str = "regular",
) -> dict | None:
    """Parse Nasdaq `/api/quote/<SYM>/info` JSON into a normalized quote dict."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    primary = data.get("primaryData")
    if not isinstance(primary, dict):
        return None
    price = _pos_float(primary.get("lastSalePrice"))
    if price is None:
        return None
    resolved_symbol = str(symbol or data.get("symbol") or "").strip().upper()
    change = _finite_float(primary.get("netChange"))
    change_pct = _finite_float(primary.get("percentageChange"))
    prev_close = (round(price - change, 4)
                  if (change is not None and price - change > 0) else None)
    if change_pct is None and prev_close:
        change_pct = round((price / prev_close - 1.0) * 100.0, 2)
    vol_raw = _finite_float(primary.get("volume"))
    volume = vol_raw if (vol_raw is not None and vol_raw >= 0) else None

    day_low, day_high = None, None
    key_stats = data.get("keyStats")
    if isinstance(key_stats, dict):
        day_range = key_stats.get("dayrange")
        if isinstance(day_range, dict) and day_range.get("value"):
            parts = str(day_range["value"]).split("-")
            if len(parts) == 2:
                lo, hi = _pos_float(parts[0]), _pos_float(parts[1])
                if lo is not None and hi is not None:
                    day_low, day_high = (lo, hi) if lo <= hi else (hi, lo)

    as_of_utc, as_of_display = _parse_et_timestamp(
        primary.get("lastTradeTimestamp") or ""
    )
    market_status = _map_nasdaq_market_status(
        data.get("marketStatus"), session_state
    )

    ext_price, ext_pct, ext_label = None, None, None
    secondary = data.get("secondaryData")
    if isinstance(secondary, dict) and market_status != "regular":
        sec_stamp = str(secondary.get("lastTradeTimestamp") or "").upper()
        pri_stamp = str(primary.get("lastTradeTimestamp") or "").upper()
        if "AFTER" in sec_stamp or "PRE" in sec_stamp:
            cand = _pos_float(secondary.get("lastSalePrice"))
            if cand is not None:
                ext_price = round(cand, 4)
                ext_pct = _finite_float(secondary.get("percentageChange"))
                ext_label = "Pre-market" if "PRE" in sec_stamp else "After-hours"
        elif ("AFTER" in pri_stamp or "PRE" in pri_stamp) and "CLOSE" in sec_stamp:
            reg_close = _pos_float(secondary.get("lastSalePrice"))
            if reg_close is not None:
                ext_price = round(price, 4)
                ext_pct = round(change_pct, 2) if change_pct is not None else None
                ext_label = "Pre-market" if "PRE" in pri_stamp else "After-hours"
                price = reg_close
                sec_chg = _finite_float(secondary.get("netChange"))
                sec_pct = _finite_float(secondary.get("percentageChange"))
                change = sec_chg
                change_pct = sec_pct
                prev_close = (round(price - sec_chg, 4)
                              if (sec_chg is not None and price - sec_chg > 0)
                              else prev_close)
                as_of_utc, as_of_display = _parse_et_timestamp(
                    secondary.get("lastTradeTimestamp") or ""
                )

    return {
        "symbol": resolved_symbol,
        "price": round(price, 4),
        "prev_close": prev_close,
        "change": round(change, 4) if change is not None else None,
        "change_pct": round(change_pct, 2) if change_pct is not None else None,
        "open": None,
        "day_high": round(day_high, 4) if day_high is not None else None,
        "day_low": round(day_low, 4) if day_low is not None else None,
        "volume": volume,
        "extended_price": ext_price,
        "extended_change_pct": (round(ext_pct, 2)
                                if ext_pct is not None else None),
        "extended_label": ext_label,
        "as_of_utc": as_of_utc,
        "as_of_display": as_of_display,
        "delayed": True,
        "source": "Nasdaq quote",
        "market_status": market_status,
    }


def _nasdaq_us_quote(
    symbol: str,
    *,
    session_state: str = "regular",
    budget: _RequestBudget | None = None,
) -> dict:
    """Fetch delayed/NLS quote for one US ticker via Nasdaq's quote info API."""
    text = _stock_host_get(
        _nasdaq_quote_url(symbol), NASDAQ_US_QUOTE_KEY,
        headers=NASDAQ_HEADERS, timeout=8, budget=budget,
    )
    quote = _parse_nasdaq_quote_payload(
        text, symbol=symbol, session_state=session_state,
    )
    if not quote:
        raise _SymbolMiss(f"no Nasdaq quote for {symbol}")
    note_provider_ok(NASDAQ_US_QUOTE_KEY)
    return quote


def _tencent_us_quotes_url(symbols) -> str:
    codes = [f"us{str(s).strip().upper()}" for s in symbols if str(s or "").strip()]
    return "https://qt.gtimg.cn/q=" + ",".join(dict.fromkeys(codes))


def _parse_tencent_us_quotes_payload(
    text: str,
    *,
    session_state: str = "regular",
) -> dict[str, dict]:
    """Parse `v_us<SYM>="..."` lines from `qt.gtimg.cn` into quote dicts."""
    out: dict[str, dict] = {}
    for chunk in (text or "").split(";"):
        if '="' not in chunk:
            continue
        left, _, right = chunk.partition('="')
        code = left.strip().split("v_", 1)[-1].strip()
        if code == "pv_none_match" or not code.startswith("us"):
            continue
        symbol = code[2:].split(".", 1)[0].upper()
        fields = right.split('"', 1)[0].split("~")
        if len(fields) < 6 or not symbol:
            continue
        price = _pos_float(fields[3])
        if price is None:
            continue
        prev_close = _pos_float(fields[4])
        open_p = _pos_float(fields[5])
        vol_raw = _finite_float(fields[6]) if len(fields) > 6 else None
        volume = vol_raw if (vol_raw is not None and vol_raw >= 0) else None
        change = _finite_float(fields[31]) if len(fields) > 31 else None
        change_pct = _finite_float(fields[32]) if len(fields) > 32 else None
        if change is None and prev_close:
            change = round(price - prev_close, 4)
        if change_pct is None and prev_close:
            change_pct = round((price / prev_close - 1.0) * 100.0, 2)
        day_high = _pos_float(fields[33]) if len(fields) > 33 else None
        day_low = _pos_float(fields[34]) if len(fields) > 34 else None
        if day_low is not None and day_high is not None and day_low > day_high:
            day_low, day_high = day_high, day_low
        as_of_utc, as_of_display = _parse_et_timestamp(
            fields[30] if len(fields) > 30 else ""
        )
        out[symbol] = {
            "symbol": symbol,
            "price": round(price, 4),
            "prev_close": round(prev_close, 4) if prev_close is not None else None,
            "change": round(change, 4) if change is not None else None,
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "open": round(open_p, 4) if open_p is not None else None,
            "day_high": round(day_high, 4) if day_high is not None else None,
            "day_low": round(day_low, 4) if day_low is not None else None,
            "volume": volume,
            "extended_price": None,
            "extended_change_pct": None,
            "extended_label": None,
            "as_of_utc": as_of_utc,
            "as_of_display": as_of_display,
            "delayed": True,
            "source": "Tencent US quote",
            "market_status": session_state,
        }
    return out


def _tencent_us_quotes_batch(
    symbols,
    *,
    session_state: str = "regular",
    budget: _RequestBudget | None = None,
) -> dict[str, dict]:
    wanted = [str(s).strip().upper() for s in symbols if str(s or "").strip()]
    if not wanted:
        return {}
    text = _stock_host_get(
        _tencent_us_quotes_url(wanted), TENCENT_US_QUOTE_KEY,
        headers=TENCENT_KLINE_HEADERS, timeout=8, budget=budget,
    )
    parsed = _parse_tencent_us_quotes_payload(text, session_state=session_state)
    if parsed:
        note_provider_ok(TENCENT_US_QUOTE_KEY)
        return parsed
    raise _SymbolMiss("Tencent returned no matching US stock quotes")


def _yahoo_us_quote(symbol: str, *, session_state: str = "regular") -> dict:
    """Last-resort intraday quote from Yahoo v8 chart metadata (skipped on 429)."""
    if not provider_available("yahoo"):
        raise RuntimeError("yahoo: cooling down after recent 429/failures")
    sym = str(symbol).strip().upper()
    enc = urllib.parse.quote(sym, safe="")
    crumb = _yahoo_session_crumb()
    crumb_param = f"&crumb={urllib.parse.quote(crumb, safe='')}" if crumb else ""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{enc}"
        f"?range=1d&interval=5m&includePrePost=true{crumb_param}"
    )
    _yahoo_pace()
    try:
        j = _get_json(url, timeout=8, headers=_yahoo_headers())
    except Exception as exc:
        unreachable, limited = _classify(exc)
        note_provider_failure("yahoo", exc, rate_limited=limited, unreachable=unreachable)
        raise RuntimeError(f"yahoo({sym}): {exc}") from exc
    result = ((j.get("chart") or {}).get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    price = _pos_float(meta.get("regularMarketPrice"))
    if price is None:
        raise _SymbolMiss(f"no Yahoo quote price for {sym}")
    prev_close = _pos_float(
        meta.get("chartPreviousClose") or meta.get("previousClose")
    )
    change = round(price - prev_close, 4) if prev_close else None
    change_pct = (round((price / prev_close - 1.0) * 100.0, 2)
                  if prev_close else None)
    day_high = _pos_float(meta.get("regularMarketDayHigh"))
    day_low = _pos_float(meta.get("regularMarketDayLow"))
    vol_raw = _finite_float(meta.get("regularMarketVolume"))
    volume = vol_raw if (vol_raw is not None and vol_raw >= 0) else None
    as_of_utc = None
    ts = meta.get("regularMarketTime")
    try:
        if ts:
            as_of_utc = datetime.fromtimestamp(
                int(ts), tz=timezone.utc
            ).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        as_of_utc = None
    note_provider_ok("yahoo")
    return {
        "symbol": sym,
        "price": round(price, 4),
        "prev_close": round(prev_close, 4) if prev_close is not None else None,
        "change": change,
        "change_pct": change_pct,
        "open": None,
        "day_high": round(day_high, 4) if day_high is not None else None,
        "day_low": round(day_low, 4) if day_low is not None else None,
        "volume": volume,
        "extended_price": None,
        "extended_change_pct": None,
        "extended_label": None,
        "as_of_utc": as_of_utc,
        "as_of_display": None,
        "delayed": True,
        "source": "Yahoo Finance",
        "market_status": session_state,
    }


def stock_quotes_batch(symbols, *, now: datetime | None = None) -> dict:
    """Fetch ~15-min-delayed US quotes for displayed stock picks.

    Cascade:
      1. Sina `hq.sinajs.cn/list=gb_...` (ONE batched HTTP call)
      2. Nasdaq `/api/quote/<SYM>/info` (paced per-symbol for misses)
      3. Tencent `qt.gtimg.cn/q=us...` (ONE batched HTTP call for misses)
      4. Yahoo `v8/finance/chart` meta (skipped when Yahoo breaker is cooling)

    Never raises to the caller: a partial or total quote outage returns a
    structured status dict (`ok` | `partial` | `unavailable` | `waiting`) so
    the daily-close stock screen is never disrupted.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment_utc = moment.astimezone(timezone.utc)
    session = us_market_session(moment_utc)
    session_state = session["state"]
    delay_note = (
        "Keyless public quotes (typically real-time Nasdaq Last Sale to ~15 min "
        "delayed). Price overlay only — never alters screen filters or ranking."
    )

    wanted = list(dict.fromkeys(
        str(s).strip().upper() for s in (symbols or []) if str(s or "").strip()
    ))
    base = {
        "delay_note": delay_note,
        "market_session": session,
        "fetched_at_utc": moment_utc.isoformat(timespec="seconds"),
    }
    if not wanted:
        return {
            **base,
            "status": "waiting",
            "message": "Waiting for screened stock picks before looking up delayed quotes.",
            "items": {},
            "errors": {},
            "quote_providers": [],
            "coverage": {"symbols": 0, "with_quotes": 0, "providers": []},
        }

    items: dict[str, dict] = {}
    symbol_errors: dict[str, list[str]] = {}
    providers_used: list[str] = []
    quote_providers: list[str] = ["stock_quote:sina"]
    budget = _RequestBudget(STOCK_QUOTE_REQUEST_BUDGET)

    def note_source(label: str) -> None:
        if label and label not in providers_used:
            providers_used.append(label)

    def add_err(sym: str, label: str, reason) -> None:
        symbol_errors.setdefault(sym, []).append(f"{label}: {str(reason)[:140]}")

    # 1. Sina US quote batch (1 request for all displayed tickers)
    try:
        sina_got = _sina_us_quotes_batch(
            wanted, session_state=session_state, budget=budget,
        )
        for sym in wanted:
            if sym in sina_got:
                items[sym] = sina_got[sym]
                note_source(sina_got[sym]["source"])
            else:
                add_err(sym, "Sina US quote", "symbol missing from batch response")
    except Exception as exc:  # noqa: BLE001 — cascade continues
        for sym in wanted:
            add_err(sym, "Sina US quote", exc)

    # 2. Nasdaq quote info for any remaining tickers
    missing = [sym for sym in wanted if sym not in items]
    if missing:
        quote_providers.append("stock_quote:nasdaq")
        workers = max(1, min(4, len(missing)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="quotes-nasdaq") as pool:
            futures = {
                pool.submit(
                    _nasdaq_us_quote, sym,
                    session_state=session_state, budget=budget,
                ): sym
                for sym in missing
            }
            for future, sym in futures.items():
                try:
                    q = future.result()
                    items[sym] = q
                    note_source(q["source"])
                except Exception as exc:  # noqa: BLE001
                    add_err(sym, "Nasdaq quote", exc)

    # 3. Tencent US quote batch for any still-missing tickers
    missing = [sym for sym in wanted if sym not in items]
    if missing:
        quote_providers.append("stock_quote:tencent")
        try:
            tencent_got = _tencent_us_quotes_batch(
                missing, session_state=session_state, budget=budget,
            )
            for sym in missing:
                if sym in tencent_got:
                    items[sym] = tencent_got[sym]
                    note_source(tencent_got[sym]["source"])
                else:
                    add_err(sym, "Tencent US quote", "symbol missing from response")
        except Exception as exc:  # noqa: BLE001
            for sym in missing:
                add_err(sym, "Tencent US quote", exc)

    # 4. Yahoo chart meta (skipped at ~0 ms when Yahoo's IP breaker is cooling)
    missing = [sym for sym in wanted if sym not in items]
    if missing:
        quote_providers.append("yahoo")
        for sym in missing:
            if not provider_available("yahoo"):
                add_err(sym, "Yahoo Finance", "provider cooling down")
                continue
            if not budget.take():
                add_err(sym, "Yahoo Finance", "request budget exhausted")
                continue
            try:
                q = _yahoo_us_quote(sym, session_state=session_state)
                items[sym] = q
                note_source(q["source"])
            except Exception as exc:  # noqa: BLE001
                add_err(sym, "Yahoo Finance", exc)

    errors_out = {
        sym: "; ".join(reasons)
        for sym, reasons in symbol_errors.items()
        if sym not in items or len(reasons) > 0
    }
    coverage = {
        "symbols": len(wanted),
        "with_quotes": len(items),
        "providers": providers_used,
    }
    if items and len(items) == len(wanted):
        status = "ok"
        message = (
            f"Delayed quotes (~15 min) loaded for all {len(wanted)} displayed "
            f"stock(s) · {session['label']}."
        )
    elif items:
        status = "partial"
        missed = len(wanted) - len(items)
        message = (
            f"Delayed quotes (~15 min) loaded for {len(items)} of {len(wanted)} "
            f"displayed stock(s); {missed} fell back to the daily close."
        )
    else:
        status = "unavailable"
        sample_reason = next(iter(errors_out.values()), "")
        first_part = sample_reason.split(";")[0] if sample_reason else ""
        message = (
            "Delayed stock quotes are unavailable"
            + (f" ({first_part})" if first_part else "")
            + "; showing latest daily closes."
        )
    return {
        **base,
        "status": status,
        "message": message,
        "items": items,
        "errors": errors_out,
        "quote_providers": quote_providers,
        "quote_requests": budget.used,
        "coverage": coverage,
    }


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


def worldbank_history(country_codes, indicator: str, years: int = 12):
    """Annual World Bank series per country, for the Economy 101 tab.

    Returns ``{ISO2: {"name": str, "series": [[year, value], ...]}}`` with each
    series sorted oldest -> newest. A year the World Bank has not published yet
    is simply absent; a country with no observations keeps an empty list. The
    call is separate from :func:`worldbank_indicator` because that one asks for
    only the latest value (`mrnev=1`) and cannot draw a chart.
    """
    codes = [str(c).strip().upper() for c in country_codes or [] if str(c).strip()]
    if not codes:
        raise ValueError("worldbank_history: no country codes")
    end_year = date.today().year
    start_year = end_year - max(2, int(years)) + 1
    ids = ";".join(codes)
    url = (
        f"https://api.worldbank.org/v2/country/{ids}/indicator/{indicator}"
        f"?format=json&date={start_year}:{end_year}&per_page=1000"
    )
    j = _get_json(url, timeout=15)
    if not isinstance(j, list) or len(j) < 2 or not isinstance(j[1], list):
        raise RuntimeError(f"worldbank: unexpected payload for {indicator}")
    out = {code: {"name": code, "series": []} for code in codes}
    for row in j[1]:
        if not isinstance(row, dict) or row.get("value") is None:
            continue
        country = row.get("country") or {}
        code = str(country.get("id") or "").strip().upper()
        if code not in out:
            continue
        try:
            year = int(str(row.get("date"))[:4])
            value = float(row["value"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        out[code]["name"] = country.get("value") or out[code]["name"]
        out[code]["series"].append([year, round(value, 2)])
    for entry in out.values():
        entry["series"].sort(key=lambda point: point[0])
    if not any(entry["series"] for entry in out.values()):
        raise RuntimeError(f"worldbank: no observations for {indicator}")
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


def _doctor_stock_history(run_checks):
    """Probe every US stock-history host for PFE with status + body head.

    The 2026-10-21 outage stayed invisible because curl -f discarded
    Tencent's WAF 501 body and no doctor section covered the stock cascade at
    all: a total outage looked like a blank tab. Each probe therefore
    captures the error body and prints the HTTP status plus the first 120
    bytes, pass or fail, so a WAF page names itself. The closing probe runs
    the real cascade for one ticker.
    """
    reset_breakers("sina")
    reset_breakers("nasdaq")
    reset_breakers("tencent_kline")

    def probe(url, headers, parse):
        try:
            body = _get(url, timeout=12, headers=headers, capture_error_body=True)
        except HttpStatusError as exc:
            raise RuntimeError(f"HTTP {exc.status} body[:120]={exc.body[:120]!r}")
        rows = parse(body)
        if not rows:
            raise RuntimeError(
                f"HTTP 200 but no rows parsed; body[:120]={body[:120]!r}")
        return (f"HTTP 200, {len(rows)} rows, last {rows[-1][0]} = {rows[-1][1]} "
                f"| body[:120]={body[:120]!r}")

    def cascade_probe():
        out = stock_history_batch(["PFE"], {"PFE": "usPFE.N"}, days=130)
        rows = out["PFE"]["rows"]
        return (f"{out['PFE']['source']}, {len(rows)} rows, "
                f"last {rows[-1][0]} = {rows[-1][1]}")

    checks = [
        ("sina daily K PFE", lambda: probe(
            _sina_daily_k_url("PFE"), SINA_KLINE_HEADERS, _parse_sina_daily_k_payload)),
        ("nasdaq historical PFE", lambda: probe(
            _nasdaq_historical_url("PFE", days=130), NASDAQ_HEADERS,
            _parse_nasdaq_historical_payload)),
    ]
    for host, base in TENCENT_KLINE_HOSTS:
        checks.append((f"tencent {host}", lambda b=base: probe(
            _tencent_kline_url(b, "usPFE.N", days=130), TENCENT_KLINE_HEADERS,
            lambda body: _parse_tencent_kline_payload(body, "usPFE.N"))))
    checks.append(("cascade PFE (batch)", cascade_probe))
    run_checks(checks, width=28)


def _doctor_stock_quotes(run_checks):
    """Probe every US delayed-quote host for PFE with HTTP status + body head."""
    reset_breakers("stock_quote")

    def probe_quote(url, headers, extract):
        try:
            body = _get(url, timeout=10, headers=headers, capture_error_body=True)
        except HttpStatusError as exc:
            raise RuntimeError(f"HTTP {exc.status} body[:120]={exc.body[:120]!r}")
        item = extract(body)
        if not item:
            raise RuntimeError(
                f"HTTP 200 but no quote parsed; body[:120]={body[:120]!r}")
        stamp = item.get("as_of_utc") or item.get("as_of_display") or "no timestamp"
        return (f"HTTP 200, price={item['price']} ({item.get('change_pct')}%) "
                f"@ {stamp} | body[:120]={body[:120]!r}")

    def cascade_probe():
        out = stock_quotes_batch(["PFE"])
        q = (out.get("items") or {}).get("PFE")
        if not q:
            raise RuntimeError(out.get("message") or "no quote returned")
        return f"{q['source']}, price={q['price']} ({q.get('change_pct')}%)"

    run_checks([
        ("sina US quote PFE", lambda: probe_quote(
            _sina_us_quotes_url(["PFE"]), SINA_KLINE_HEADERS,
            lambda body: _parse_sina_us_quotes_payload(body).get("PFE"))),
        ("nasdaq quote PFE", lambda: probe_quote(
            _nasdaq_quote_url("PFE"), NASDAQ_HEADERS,
            lambda body: _parse_nasdaq_quote_payload(body, symbol="PFE"))),
        ("tencent US quote PFE", lambda: probe_quote(
            _tencent_us_quotes_url(["PFE"]), TENCENT_KLINE_HEADERS,
            lambda body: _parse_tencent_us_quotes_payload(body).get("PFE"))),
        ("quote cascade PFE", cascade_probe),
    ], width=28)


def fetch_news(candidates, *, days=7, per_symbol=3):
    """Headlines for the displayed stock picks (see news.py for the rules).

    Imported lazily because news.py uses this module's transport and provider
    breakers; a top-level import would be circular. Kept as a thin wrapper so
    app.py can call the same `backend.fetch_news(...)` in live and demo modes.
    """
    import news

    return news.fetch_news(candidates, days=days, per_symbol=per_symbol)


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

    def _wb_history_probe(codes, indicator, label):
        out = worldbank_history(codes, indicator, years=12)
        bits = []
        for code in codes:
            series = (out.get(code) or {}).get("series") or []
            if not series:
                bits.append(f"{code}: no data")
                continue
            year, value = series[-1]
            bits.append(f"{code} {year}={value} ({len(series)}y)")
        return f"{label}: " + " | ".join(bits)

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
            ("worldbank_history", lambda: len(worldbank_history(
                ["US", "MY"], "NY.GDP.MKTP.KD.ZG", years=5)["MY"]["series"])),
            ("rate:DFF", lambda: fred_series("DFF", years=1)[-1]),
            ("rate:ECBDFR", lambda: fred_series("ECBDFR", years=1)[-1]),
            ("curve:T10Y2Y", lambda: fred_series("T10Y2Y", years=1)[-1]),
            ("breakeven:T10YIE", lambda: fred_series("T10YIE", years=1)[-1]),
            ("quote:^spx", lambda: stooq_history("^spx", days=30)[-1]),
            ("quote:cl.f", lambda: stooq_history("cl.f", days=30)[-1]),
            ("quote:xauusd", lambda: stooq_history("xauusd", days=30)[-1]),
            ("stock:PFE", lambda: stock_history_batch(
                ["PFE"], {"PFE": "usPFE.N"}, days=130)["PFE"]["source"]),
            ("stock_quote:PFE", lambda: stock_quotes_batch(
                ["PFE"])["items"]["PFE"]["source"]),
        ])
        print("provider breakers:")
        _print_breakers()

    def doctor():
        reset_breakers()
        now = datetime.now(timezone.utc).astimezone()
        print(f"econ source doctor — {now.isoformat(timespec='seconds')}")
        print(f"curl: {CURL_BIN or 'NOT FOUND (urllib only)'} | yahoo pace "
              f"{YAHOO_MIN_INTERVAL}s | yahoo cookie {'on' if YAHOO_USE_COOKIE else 'off'}")

        print("\n[1/11] FX & macro")
        _run_checks([
            ("frankfurter.app", lambda: _get_json(
                "https://api.frankfurter.app/latest?from=USD&to=EUR,JPY")["date"]),
            ("frankfurter.dev", lambda: _get_json(
                "https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR,JPY")["date"]),
            ("worldbank CPI", lambda: worldbank_indicator(["US"], "FP.CPI.TOTL.ZG")[0]["value"]),
            ("worldbank GDP", lambda: worldbank_indicator(["US"], "NY.GDP.MKTP.KD.ZG")[0]["value"]),
            ("wb history growth (US/MY)", lambda: _wb_history_probe(
                ["US", "MY"], "NY.GDP.MKTP.KD.ZG", "growth")),
            ("wb history trade (US/MY)", lambda: _wb_history_probe(
                ["US", "MY"], "NE.TRD.GNFS.ZS", "trade")),
        ], width=26)

        print("\n[2/11] Rates, curve & their official fallbacks")
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

        print("\n[3/11] FRED quote series (mapped to board cards, including fallbacks)")
        mapped_fred = {sid for sid, _age in FRED_QUOTE_MAP.values()}
        mapped_fred.update(
            sid for fallbacks in FRED_QUOTE_FALLBACKS.values()
            for sid, _age in fallbacks
        )
        _run_checks([
            (f"fred {sid}", lambda s=sid, m=sid.endswith("USDM"): _fred_probe(s, m))
            for sid in sorted(mapped_fred)
        ], width=24)

        print("\n[4/11] FRED candidate series (not wired in yet)")
        if FRED_PROBE_CANDIDATES:
            _run_checks([
                (f"fred {sid}", lambda s=sid: _fred_probe(s, monthly=s.endswith("USDM")))
                for sid in FRED_PROBE_CANDIDATES
            ], width=24)
        else:
            print("  (no unverified candidates)")

        print("\n[5/11] Yahoo Finance (cookie + crumb handshake, charts, spark batch)")
        _doctor_yahoo(_run_checks)

        print("\n[6/11] CNBC quote candidates (international indices)")
        _doctor_cnbc(_run_checks)

        print("\n[7/11] Stooq & CoinGecko")
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

        print("\n[8/11] Tencent / Sina (indices FRED does not publish)")
        reset_breakers("tencent")
        reset_breakers("sina")
        clear_asia_cache()
        def _asia_probe(sym):
            h = _asia_history(sym, days=30)
            src = _LAST_SOURCE.get(sym, ("?", None))[0]
            return f"{src} last {h[-1][0]} = {h[-1][1]}"
        _run_checks([
            ("shanghai ^shc", lambda: _asia_probe("^shc")),
            ("hang seng ^hsi", lambda: _asia_probe("^hsi")),
            ("ftse ^ukx", lambda: _asia_probe("^ukx")),
            ("dax ^dax", lambda: _asia_probe("^dax")),
            ("euro stoxx ^stx", lambda: _asia_probe("^stx")),
        ], width=24)

        print("\n[9/11] US stock history (Stocks tab; Sina -> Nasdaq -> Tencent hosts, PFE probe)")
        _doctor_stock_history(_run_checks)

        print("\n[10/11] Stock headline feeds (Stocks tab; Google -> Bing -> Yahoo)")
        import news
        reset_breakers("google_news")
        reset_breakers("bing_news")
        reset_breakers("yahoo_news")
        sample = (("PFE", "Pfizer"),)   # a ticker in the configured watchlist
        probe_checks = []
        for provider, label, builder in news._URL_BUILDERS:
            def _probe(b=builder, name=sample[0][1], sym=sample[0][0]):
                body = _get(b(name, sym, 7), timeout=8)
                items = news.parse_feed(body)
                if not items:
                    raise RuntimeError("empty or unparseable RSS")
                newest = max((i["published"] for i in items if i["published"]),
                             default=None)
                stamp = newest.isoformat(timespec="minutes") if newest else "no dates"
                return f"{len(items)} item(s), newest {stamp}"
            probe_checks.append((label, _probe))
        _run_checks(probe_checks, width=24)

        print("\n[11/11] US stock delayed quotes (Stocks tab; Sina -> Nasdaq -> Tencent, PFE probe)")
        _doctor_stock_quotes(_run_checks)

        print("\nProvider breakers after the probe:")
        _print_breakers()

    (doctor if "--doctor" in sys.argv else self_test)()
