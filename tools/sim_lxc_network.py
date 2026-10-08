"""Simulate the LXC's observed network profile and time a full warm cycle.

The sandbox can't reach any of these hosts, and its failures are instant, so
this harness fakes the transport (`sources._get`) with the *measured* latencies
from the 2026-10-07 deploy on 192.168.0.149:

  frankfurter 1.0s OK      worldbank 3.4s OK        fred rates/curve 0.1-0.6s OK
  fred DCOILWTI/DCOILBRENTEU -> HTTP 404 (discontinued series)
  yahoo: first call 0.9s OK, then HTTP 429 (per-IP rate limit)
  stooq.com / stooq.pl: TCP timeout after 5.0s
  coingecko pax-gold 0.6s OK

Run it against any checkout to compare warm-up cost:

    python3 tools/sim_lxc_network.py dashboard
    python3 tools/sim_lxc_network.py /tmp/old/dashboard
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

FRED_DEAD = {"DCOILWTI", "DCOILBRENTEU"}
STOOQ_TIMEOUT = 5.0
YAHOO_429_DELAY = 0.8

_state = {"yahoo_ok": 1}  # first Yahoo call succeeds, the rest get 429


def _days(n, start=100.0, step=0.5):
    today = date.today()
    return [((today - timedelta(days=i)).isoformat(), round(start + i * step, 2))
            for i in range(n)][::-1]


def fake_get(url, timeout=None, headers=None, connect_timeout=5):
    if "fredgraph.csv" in url:
        sid = url.split("id=", 1)[1].split("&", 1)[0]
        time.sleep(0.15)
        if sid in FRED_DEAD:
            raise RuntimeError("curl: (22) The requested URL returned error: 404 HTTP 404")
        rows = _days(30)
        return "observation_date,VALUE\n" + "".join(f"{d},{v}\n" for d, v in rows)
    if "newyorkfed.org" in url:
        time.sleep(0.15)
        return json.dumps({"refRates": [
            {"effectiveDate": d, "percentRate": v} for d, v in _days(10)]})
    if "ecb.europa.eu" in url:
        time.sleep(0.15)
        return "TIME_PERIOD,OBS_VALUE\n" + "".join(f"{d},{v}\n" for d, v in _days(10))
    if "treasury.gov" in url:
        time.sleep(0.3)
        return ("Date,2 Yr,10 Yr\n" +
                "".join(f"{d},{v},{v + 0.5}\n" for d, v in _days(10)))
    if "frankfurter.app/latest" in url:
        time.sleep(1.0)
        return json.dumps({"base": "USD", "date": date.today().isoformat(),
                           "rates": {c: 1.0 + i / 100 for i, c in enumerate(
                               url.split("to=", 1)[1].split(","))}})
    if "frankfurter" in url:  # time series
        time.sleep(1.0)
        rates = {d: {c: 1.0 + i / 100 for c in
                     ("EUR", "JPY", "CNY", "GBP", "AUD", "CHF", "INR", "MYR")}
                 for i, (d, _) in enumerate(_days(90))}
        return json.dumps({"rates": rates})
    if "worldbank.org" in url:
        time.sleep(3.4)
        return json.dumps([[{"id": "FP.CPI.TOTL.ZG"}],
                           [{"country": {"id": c, "value": c}, "date": "2024",
                             "value": 2.5} for c in ("US", "EMU", "CN", "JP", "GB", "IN", "DE")]])
    if "stooq." in url:
        # curl gives up at whichever limit hits first (connect-timeout vs max-time)
        time.sleep(min(STOOQ_TIMEOUT, connect_timeout or STOOQ_TIMEOUT))
        raise RuntimeError("curl: (28) Failed to connect to stooq.pl port 443 "
                           "after 5000 ms: Timeout was reached")
    if "finance.yahoo.com/v7/finance/spark" in url:
        time.sleep(YAHOO_429_DELAY)
        if _state["yahoo_ok"] > 0:
            _state["yahoo_ok"] -= 1
            out = []
            for ysym in url.split("symbols=", 1)[1].split("&", 1)[0].split("%2C"):
                ts = [1_700_000_000 + i * 86400 for i in range(5)]
                out.append({"symbol": ysym, "response": [{
                    "timestamp": ts,
                    "indicators": {"quote": [{"close": [100 + i for i in range(5)]}]}}]})
            return json.dumps({"spark": {"result": out}})
        raise RuntimeError("curl: (22) The requested URL returned error: 429 HTTP 429")
    if "finance.yahoo.com/v8/finance/chart" in url:
        time.sleep(YAHOO_429_DELAY)
        if _state["yahoo_ok"] > 0:
            _state["yahoo_ok"] -= 1
            ts = [1_700_000_000 + i * 86400 for i in range(5)]
            return json.dumps({"chart": {"result": [{
                "timestamp": ts,
                "indicators": {"quote": [{"close": [100 + i for i in range(5)]}]}}]}})
        raise RuntimeError("curl: (22) The requested URL returned error: 429 HTTP 429")
    if "coingecko.com" in url:
        time.sleep(0.6)
        prices = [[1_700_000_000_000 + i * 86_400_000, 3000 + i] for i in range(30)]
        return json.dumps({"prices": prices})
    raise AssertionError(f"unhandled url in sim: {url}")


sources._get = fake_get
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
