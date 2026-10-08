"""Simulate the LXC's measured network profile and time one warm cycle.

Ground truth pasted from `sources.py --doctor` on `root@econ` at
2026-10-08T00:18:04Z (not re-probed from this sandbox):

  frankfurter.app/.dev      0.69 / 0.72s
  World Bank CPI / GDP       1.81 / 0.48s
  healthy FRED series        0.10-0.78s (daily/policy mostly 0.15-0.72s)
  dead FRED IDs               12.60-25.42s with the old urllib retry/widen path
  Yahoo spark batch          HTTP 429 in 0.30s (cookie handshake succeeded)
  stooq endpoints            21.06s (curl TCP timeout + urllib retry)
  CoinGecko metals           0.47-0.54s

The harness fakes the low-level curl/urllib seams, leaving each checkout's
real `_get()` policy, provider breakers, cascade, pre-flight, and warm_all()
intact. For the current transport fix a TCP failure must stop after curl; the
baseline retries through urllib and pays the observed extra time.

Run against the current checkout and an older checkout, e.g.:

    python3 tools/sim_lxc_network.py dashboard
    python3 tools/sim_lxc_network.py /tmp/econ-v03-baseline/dashboard
"""

import json
import os
import sys
import time
from datetime import date, timedelta


dashboard_dir = sys.argv[1] if len(sys.argv) > 1 else "dashboard"
sys.path.insert(0, os.path.abspath(dashboard_dir))
os.environ["ECON_CACHE_FILE"] = "/tmp/econ-sim-cache.json"
if os.path.exists(os.environ["ECON_CACHE_FILE"]):
    os.remove(os.environ["ECON_CACHE_FILE"])

import app  # noqa: E402
import sources  # noqa: E402

# Measured successful-response latencies, seconds. `frankfurter` is paid once
# for latest and once for the 90-day series; World Bank CPI/GDP differ materially.
FRANKFURTER_DELAY = {"app": 0.69, "dev": 0.72}
WORLD_BANK_DELAY = {"CPI": 1.81, "GDP": 0.48}
FRED_DELAY = {
    "SP500": 0.19,
    "NASDAQ100": 0.15,
    "DJIA": 0.22,
    "NIKKEI225": 0.16,
    "DCOILBRENTEU": 0.15,
    "PCOPPUSDM": 0.10,
    "PWHEAMTUSDM": 0.11,
    "POILWTIUSDM": 0.71,
    "POILBREUSDM": 0.64,
    "PALUMUSDM": 0.78,
    "PNICKUSDM": 0.71,
    "DFF": 0.64,
    "ECBDFR": 0.72,
    "T10Y2Y": 0.64,
    "T10YIE": 0.62,
}
FRED_DEAD = {"DCOILWTI", "PSILVUSDM", "GOLDAMGBD228NLBM"}
FRED_404_CURL_DELAY = 0.20
FRED_404_URLLIB_DELAY = {
    # Together with the 0.20 s curl response, these reproduce the measured
    # per-request costs: 24.47 s for two WTI 404 windows, 12.60 s for silver,
    # and 25.42 s for gold's two-window probe.
    "DCOILWTI": 12.035,
    "PSILVUSDM": 12.40,
    "GOLDAMGBD228NLBM": 12.51,
}
STOOQ_CURL_LIMIT = 5.0
STOOQ_URLLIB_PER_ADDRESS = 8.0   # timeout=8; two address attempts -> 16s
YAHOO_SPARK_429_DELAY = 0.30
YAHOO_CHART_FIRST_DELAY = 0.90    # previous live run's first v8 chart success
YAHOO_CHART_429_DELAY = 0.30
COINGECKO_DELAY = 0.50

_FRED_LEVELS = {
    "SP500": 7801.77, "NASDAQ100": 31224.69, "DJIA": 51179.87,
    "NIKKEI225": 70035.71, "DCOILBRENTEU": 125.44,
    "POILWTIUSDM": 79.71, "POILBREUSDM": 83.73,
    "PCOPPUSDM": 13542.82, "PWHEAMTUSDM": 228.74,
    "PALUMUSDM": 3158.27, "PNICKUSDM": 16632.36,
    "DFF": 3.88, "ECBDFR": 2.50, "T10Y2Y": 0.51, "T10YIE": 2.36,
}
_state = {"yahoo_chart_ok": 1}


def _days(n, start=100.0, step=0.5, end=None):
    end = end or date.today()
    return [((end - timedelta(days=i)).isoformat(), round(start + (n - i - 1) * step, 2))
            for i in range(n)][::-1]


def _monthly_rows(series_id, n=60):
    last = date.today() - timedelta(days=99)  # measured lag of IMF monthly cards
    base = _FRED_LEVELS.get(series_id, 100.0)
    return [((last - timedelta(days=30 * i)).isoformat(), round(base * (1 + i * 0.002), 4))
            for i in range(n - 1, -1, -1)]


def _json_response(payload):
    return json.dumps(payload)


def _fake_request(url, timeout=None, headers=None, connect_timeout=5, layer="curl"):
    """Return a measured-latency response or raise the measured transport error."""
    if "fredgraph.csv" in url:
        sid = url.split("id=", 1)[1].split("&", 1)[0]
        if sid in FRED_DEAD:
            if layer == "curl":
                time.sleep(FRED_404_CURL_DELAY)
                raise RuntimeError(
                    "curl: (22) The requested URL returned error: 404 HTTP 404")
            time.sleep(FRED_404_URLLIB_DELAY.get(sid, 12.0))
            raise RuntimeError("urllib: HTTP Error 404: Not Found")

        time.sleep(FRED_DELAY.get(sid, 0.40))
        monthly = sid.endswith("USDM")
        rows = _monthly_rows(sid) if monthly else _days(
            30, start=_FRED_LEVELS.get(sid, 100.0), step=0.01)
        return "observation_date,VALUE\n" + "".join(f"{d},{v}\n" for d, v in rows)

    if "newyorkfed.org" in url:
        time.sleep(0.51)
        return _json_response({"refRates": [
            {"effectiveDate": d, "percentRate": v} for d, v in _days(10, start=3.88)]})

    if "ecb.europa.eu" in url:
        time.sleep(0.60)
        return "TIME_PERIOD,OBS_VALUE\n" + "".join(
            f"{d},{v}\n" for d, v in _days(10, start=2.5))

    if "treasury.gov" in url:
        time.sleep(0.30)
        if "daily_treasury_real_yield_curve" in url:
            # Reproduce the capitalization mismatch seen in the live CSV.
            return ("Date,5 YR,10 YR,20 YR,30 YR\n" +
                    "".join(f"{d},1.0,2.0,2.2,2.4\n" for d, _ in _days(10)))
        return ("Date,2 Yr,10 Yr\n" +
                "".join(f"{d},3.0,3.5\n" for d, _ in _days(10)))

    if "frankfurter.app/latest" in url:
        time.sleep(FRANKFURTER_DELAY["app"])
        return _json_response({"base": "USD", "date": date.today().isoformat(),
                               "rates": {c: 1.0 + i / 100 for i, c in enumerate(
                                   url.split("to=", 1)[1].split(","))}})
    if "api.frankfurter.dev" in url:
        time.sleep(FRANKFURTER_DELAY["dev"])
        return _json_response({"base": "USD", "date": date.today().isoformat(),
                               "rates": {c: 1.0 + i / 100 for i, c in enumerate(
                                   url.split("symbols=", 1)[1].split(","))}})
    if "frankfurter" in url:  # historical timeseries request
        time.sleep(FRANKFURTER_DELAY["app"])
        rates = {d: {c: 1.0 + i / 100 for c in
                     ("EUR", "JPY", "CNY", "GBP", "AUD", "CHF", "INR", "MYR")}
                 for i, (d, _) in enumerate(_days(90))}
        return _json_response({"rates": rates})

    if "worldbank.org" in url:
        is_cpi = "FP.CPI.TOTL.ZG" in url
        time.sleep(WORLD_BANK_DELAY["CPI" if is_cpi else "GDP"])
        indicator = "FP.CPI.TOTL.ZG" if is_cpi else "NY.GDP.MKTP.KD.ZG"
        return _json_response([[{"id": indicator}],
                               [{"country": {"id": c, "value": c}, "date": "2024",
                                 "value": 2.5} for c in
                                ("US", "EMU", "CN", "JP", "GB", "IN", "DE")]])

    if "stooq." in url:
        if layer == "curl":
            wait = min(STOOQ_CURL_LIMIT, float(timeout or STOOQ_CURL_LIMIT),
                       float(connect_timeout or STOOQ_CURL_LIMIT))
            time.sleep(wait)
            host = url.split("//", 1)[1].split("/", 1)[0]
            raise RuntimeError(
                f"curl: (28) Failed to connect to {host} port 443 after "
                f"{int(wait * 1000)} ms: Timeout was reached")
        # urllib's 8 s socket timeout is paid once per resolved A record.
        time.sleep(min(float(timeout or 8) * 2, STOOQ_URLLIB_PER_ADDRESS * 2))
        raise RuntimeError("urllib: socket timed out while connecting to Stooq")

    if "finance.yahoo.com/v7/finance/spark" in url:
        time.sleep(YAHOO_SPARK_429_DELAY)
        raise RuntimeError("curl: (22) The requested URL returned error: 429 HTTP 429")

    if "finance.yahoo.com/v8/finance/chart" in url:
        if _state["yahoo_chart_ok"] > 0:
            time.sleep(YAHOO_CHART_FIRST_DELAY)
            _state["yahoo_chart_ok"] -= 1
            ts = [1_700_000_000 + i * 86400 for i in range(5)]
            return _json_response({"chart": {"result": [{
                "timestamp": ts,
                "indicators": {"quote": [{"close": [100 + i for i in range(5)]}]}}]}})
        time.sleep(YAHOO_CHART_429_DELAY)
        raise RuntimeError("curl: (22) The requested URL returned error: 429 HTTP 429")

    if "coingecko.com" in url:
        time.sleep(COINGECKO_DELAY)
        start_ms = int((time.time() - 29 * 86400) * 1000)
        prices = [[start_ms + i * 86_400_000, 4000 + i] for i in range(30)]
        return _json_response({"prices": prices})

    raise AssertionError(f"unhandled URL in network simulation ({layer}): {url}")


def fake_curl_get(url, timeout=None, headers=None, connect_timeout=5):
    return _fake_request(url, timeout=timeout, headers=headers,
                         connect_timeout=connect_timeout, layer="curl")


def fake_urllib_get(url, timeout=None, headers=None):
    return _fake_request(url, timeout=timeout, headers=headers,
                         connect_timeout=None, layer="urllib")


# Keep the real checkout's _get() in the path so the transport fallback
# behavior itself is measured. Older revisions have the same low-level seams;
# the fallback assignment is for compatibility with any earlier copy lacking them.
if hasattr(sources, "_curl_get") and hasattr(sources, "_urllib_get"):
    sources._curl_get = fake_curl_get
    sources._urllib_get = fake_urllib_get
else:  # pragma: no cover - compatibility with older one-function transports
    sources._get = lambda url, timeout=None, headers=None, **kwargs: _fake_request(
        url, timeout=timeout, headers=headers, layer="curl")
if not getattr(sources, "CURL_BIN", None):
    sources.CURL_BIN = "/simulated/curl"
if hasattr(sources, "YAHOO_USE_COOKIE"):
    sources.YAHOO_USE_COOKIE = False  # no real cookie handshake in the sim


t0 = time.time()
app.warm_all(force=True)
dt = time.time() - t0

filled = [k for k, v in app.CACHE.items() if (v or {}).get("data") is not None]
print(f"\nRESULT warm_all(): {dt:.1f}s | {len(filled)}/{len(app.REGISTRY)} keys filled")
s = app.summary()
print("  indices     : "
      f"{sum(q is not None for q in s['indices'])}/{len(s['indices'])}")
print("  commodities : "
      f"{sum(q is not None for q in s['commodities'])}/{len(s['commodities'])}")
print(f"  fx          : {len(s['fx'])}/8")
print(f"  cpi/gdp     : {len(s['cpi'])}/{len(s['gdp'])}")
print(f"  curve/breakeven: {len(s['curve'])}/{len(s['breakeven'])} points")
if hasattr(sources, "provider_status"):
    for p in sources.provider_status():
        print(f"  breaker {p['provider']}: {p['state']} retry_in={p['retry_in']}s "
              f"fails={p['fails']}")
