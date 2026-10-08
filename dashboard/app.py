#!/usr/bin/env python3
"""econ dashboard — a self-hosted, glanceable world-economy board.

Python stdlib only (no pip deps). Runs as a systemd service on the LXC.

  ECON_PORT=8080     listen port (default 8080)
  ECON_HOST=0.0.0.0  bind address
  ECON_DEMO=1        serve bundled sample data (no network) for previews
  ECON_REFRESH=300   background cache refresh cadence (seconds)

Endpoints:  /            the dashboard
            /api/summary data as JSON
            /healthz     liveness probe
"""

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import briefing
import config
import render
import sources

if config.DEMO:
    import demo as backend
else:
    backend = sources

# ---------------------------------------------------------------------------
# Cache: key -> {"data": ..., "at": epoch, "ttl": seconds, "err": str | None}
# ---------------------------------------------------------------------------

CACHE: dict = {}
LOCK = threading.Lock()


def load_disk_cache():
    """Restore last known good data from disk so service restarts aren't blank."""
    if config.DEMO or not config.CACHE_FILE:
        return
    try:
        p = Path(config.CACHE_FILE)
        if not p.is_file():
            return
        raw = json.loads(p.read_text("utf-8"))
        if isinstance(raw, dict):
            with LOCK:
                for k, v in raw.items():
                    if isinstance(v, dict) and v.get("data") is not None:
                        CACHE[k] = v
            print(f"[cache] restored {len(CACHE)} entries from {config.CACHE_FILE}",
                  flush=True)
    except Exception as exc:
        print(f"[cache] disk restore skipped: {exc}", flush=True)


def save_disk_cache():
    """Persist non-empty cache entries atomically to disk."""
    if config.DEMO or not config.CACHE_FILE:
        return
    try:
        p = Path(config.CACHE_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        with LOCK:
            snap = {
                k: {"data": v["data"], "at": v.get("at", 0), "ttl": v.get("ttl", 600), "err": None}
                for k, v in CACHE.items()
                if isinstance(v, dict) and v.get("data") is not None
            }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(snap), "utf-8")
        os.replace(tmp, p)
    except Exception:
        pass


def cache_get(key):
    with LOCK:
        return CACHE.get(key)


def cache_put(key, data, err=None, ttl=600):
    with LOCK:
        CACHE[key] = {"data": data, "at": time.time(), "ttl": ttl, "err": err}
    if data is not None and err is None:
        save_disk_cache()


def cache_valid(entry) -> bool:
    return bool(entry) and entry.get("data") is not None and \
        (time.time() - entry["at"]) < entry.get("ttl", 600)


def fetch_into_cache(key, fn, ttl):
    """Run fn() and store result; on failure keep last good data and record err."""
    try:
        data = fn()
        cache_put(key, data, err=None, ttl=ttl)
        print(f"[cache] {key}: ok ({len(data) if hasattr(data, '__len__') else data})",
              flush=True)
    except Exception as exc:  # noqa: BLE001 — one bad source must never kill the board
        prev = cache_get(key) or {}
        keep = prev.get("data")
        cache_put(key, keep, err=f"{type(exc).__name__}: {exc}", ttl=ttl)
        print(f"[cache] {key}: FAILED {exc} (kept={'stale' if keep else 'nothing'})",
              flush=True)


# ---------------------------------------------------------------------------
# Fetch registry — fast macro sources first, then market quotes
# ---------------------------------------------------------------------------

def build_registry():
    reg = []  # (key, fn, ttl)

    # 1. FX (Frankfurter ECB — fast, single bulk call)
    fx_codes = [c for _, c in config.FX_CURRENCIES]

    def fx_quotes():
        latest = backend.fx_latest(fx_codes)
        series = backend.fx_series(fx_codes, days=90)
        quotes = []
        for label, code in config.FX_CURRENCIES:
            rate = latest["rates"].get(code)
            hist = [v for _, v in series.get(code, [])]
            if rate is None or len(hist) < 2:
                continue
            prev = hist[-2]
            chg = rate - prev
            quotes.append({
                "name": f"{label} ({code})",
                "close": rate,
                "chg": chg,
                "chg_pct": (chg / prev * 100.0) if prev else 0.0,
                "spark": hist[-60:],
                "date": latest["date"],
                "source": "ECB",
                "stale": False,
            })
        return quotes
    reg.append(("fx", fx_quotes, config.TTL["fx"]))

    # 2. World Bank annual macro (fast JSON API)
    reg.append(("wb_cpi", lambda: backend.worldbank_indicator(
        config.WB_COUNTRIES, config.WB_CPI), config.TTL["wb"]))
    reg.append(("wb_gdp", lambda: backend.worldbank_indicator(
        config.WB_COUNTRIES, config.WB_GDP), config.TTL["wb"]))

    # 3. Central-bank policy rates, yield curve, breakeven inflation
    for sid, _title in config.FRED_POLICY:
        reg.append((f"fred:{sid}", lambda s=sid: backend.fred_series(s, years=1),
                    config.TTL["fred"]))

    curve_sid, _, curve_years = config.FRED_CURVE
    reg.append(("fred:curve", lambda s=curve_sid, y=curve_years: backend.fred_series(
        s, years=y), config.TTL["fred"]))
    be_sid, _ = config.FRED_BREAKEVEN
    reg.append(("fred:breakeven", lambda s=be_sid: backend.fred_series(
        s, years=1), config.TTL["fred"]))

    # 4. Global indices & commodities (multi-source: Yahoo -> FRED -> Stooq)
    for name, sym in config.INDICES + config.COMMODITIES:
        def make(label=name, symbol=sym):
            return lambda: sources.quote_from_history(
                label,
                backend.stooq_history(symbol, days=config.SPARK_DAYS),
                symbol=symbol,
            )
        reg.append((f"stooq:{sym}", make(), config.TTL["market"]))

    return reg


REGISTRY = build_registry()


# ---------------------------------------------------------------------------
# Background refresher — concurrent warming so one slow host never blocks all
# ---------------------------------------------------------------------------

def warm_all(force=False):
    todo = []
    for key, fn, ttl in REGISTRY:
        entry = cache_get(key)
        if not force and cache_valid(entry):
            continue
        todo.append((key, fn, ttl))

    if not todo:
        return []

    # Yahoo rate-limits per IP, so pull every stale market symbol in ONE batched
    # request before the workers fan out (15 requests -> 1). Dead optional hosts
    # are probed up front so no worker pays their TCP timeout.
    if not config.DEMO:
        stale_syms = [k.split(":", 1)[1] for k, _, _ in todo if k.startswith("stooq:")]
        if stale_syms:
            try:
                if hasattr(backend, "warm_providers"):
                    backend.warm_providers()
                if hasattr(backend, "prefetch_yahoo"):
                    backend.prefetch_yahoo(stale_syms, days=config.SPARK_DAYS)
            except Exception:  # pragma: no cover — pre-flight is best effort
                traceback.print_exc()

    workers = 1 if config.DEMO else 4
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="warm") as pool:
        futures = [pool.submit(fetch_into_cache, key, fn, ttl) for key, fn, ttl in todo]
        for fut in futures:
            try:
                fut.result()
            except Exception:
                traceback.print_exc()
    return []


def refresher_loop():
    while True:
        started = time.time()
        try:
            warm_all()
        except Exception:  # pragma: no cover
            traceback.print_exc()
        elapsed = time.time() - started
        time.sleep(max(30, config.REFRESH_SECONDS - elapsed))


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def get_data(key):
    entry = cache_get(key)
    return (entry or {}).get("data")


def summary():
    curve_sid, curve_title, curve_years = config.FRED_CURVE
    be_sid, be_title = config.FRED_BREAKEVEN
    policy_series = {sid: get_data(f"fred:{sid}") for sid, _ in config.FRED_POLICY}
    with LOCK:
        errors = [f"{k}: {v['err']}" for k, v in CACHE.items() if v.get("err")]
    providers = backend.provider_status() if hasattr(backend, "provider_status") else []
    out = {
        "indices": [get_data(f"stooq:{s}") for _, s in config.INDICES],
        "commodities": [get_data(f"stooq:{s}") for _, s in config.COMMODITIES],
        "fx": get_data("fx") or [],
        "cpi": get_data("wb_cpi") or [],
        "gdp": get_data("wb_gdp") or [],
        "policy_pairs": config.FRED_POLICY,
        "policy_series": policy_series,
        "curve": get_data("fred:curve") or [],
        "curve_meta": (curve_sid, curve_title, curve_years),
        "breakeven": get_data("fred:breakeven") or [],
        "breakeven_meta": (be_sid, be_title),
        "errors": errors[:6],
        "providers": providers,
    }
    out["briefing"] = briefing.build_briefing(out)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "econ/0.7"

    def log_message(self, fmt, *args):  # quieter logs
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (stdlib naming)
        path = urlparse(self.path).path
        try:
            if path == "/":
                s = summary()
                page = render.build_page(
                    s,
                    demo=config.DEMO,
                    generated_at=datetime.now(timezone.utc).astimezone(),
                    refresh=config.REFRESH_SECONDS,
                    errors=s.get("errors"),
                    providers=s.get("providers"),
                )
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif path == "/api/summary":
                s = summary()
                body = json.dumps(s, default=str, indent=1).encode()
                self._send(200, body, "application/json")
            elif path == "/healthz":
                self._send(200, b"ok\n", "text/plain")
            else:
                self._send(404, b"not found\n", "text/plain")
        except BrokenPipeError:
            pass
        except Exception:  # pragma: no cover
            traceback.print_exc()
            try:
                self._send(500, b"internal error\n", "text/plain")
            except Exception:
                pass


def main():
    load_disk_cache()
    t = threading.Thread(target=refresher_loop, name="refresher", daemon=True)
    t.start()
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    mode = "DEMO (sample data)" if config.DEMO else "LIVE (multi-source free APIs)"
    print(f"econ dashboard listening on http://{config.HOST}:{config.PORT}  [{mode}]",
          flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
