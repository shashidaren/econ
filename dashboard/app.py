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
import math
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import briefing
import config
import render
import sources
import stocks as stock_screener

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
    real_sid, _ = config.FRED_REAL
    reg.append(("fred:real", lambda s=real_sid: backend.fred_series(
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

    # 5. US short-term stock screen: bounded universe, technical/risk-only.
    def stock_screen_data():
        symbols = [row[0] for row in config.STOCK_UNIVERSE]
        vendor_symbols = {row[0]: row[3] for row in config.STOCK_UNIVERSE}
        histories = backend.stock_history_batch(
            symbols,
            vendor_symbols=vendor_symbols,
            days=config.STOCK_HISTORY_SESSIONS,
        )
        return stock_screener.build_screen(
            config.STOCK_UNIVERSE,
            histories,
            max_price=config.STOCK_MAX_PRICE,
            min_price=config.STOCK_MIN_PRICE,
            min_avg_dollar_volume=config.STOCK_MIN_AVG_DOLLAR_VOLUME,
            limit=config.STOCK_MAX_RESULTS,
        )
    reg.append(("stock_screen", stock_screen_data, config.TTL["stocks"]))

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
        # The stock screen fans out across a fixed watchlist. If every provider
        # fails, honour its 1 h TTL even with no last-good cache so a 5 min board
        # refresh does not repeat the full stock-source cascade.
        if (not force and key == "stock_screen" and entry and entry.get("err")
                and (time.time() - entry.get("at", 0)) < ttl):
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


def _stock_breaker_family(name):
    """Map a breaker key to the stock-history cascade stage that shares it."""
    if name == "tencent_kline":
        return "tencent_kline"
    if name == "yahoo":
        return "yahoo"
    if name.startswith("stooq"):
        return "stooq"
    return None


def _stock_provider_issues(providers, screen=None):
    """Keep stock-tab diagnostics scoped to its three history providers.

    When `screen` records the providers its latest history refresh actually
    needed (`history_providers`), only those are reported. The `yahoo` and
    `stooq:*` breakers are shared with non-stock features (index quotes, spark
    charts), so their outages must not degrade a fully covered refresh that
    never touched them. Screens without that scope keep the wider view.
    """
    scope = (screen or {}).get("history_providers")
    scope = {str(name) for name in scope} if scope is not None else None
    stock_names = []
    for item in providers or []:
        name = str(item.get("provider") or "")
        family = _stock_breaker_family(name)
        if family is None:
            continue
        if scope is not None and family not in scope:
            continue
        stock_names.append(dict(item))
    return stock_names


def _with_stock_refresh_meta(screen, entry, *, now=None):
    """Tell the page how often this screen re-checks providers, and when next.

    The stock screen is a daily-close screen: its numbers legitimately stay
    identical between U.S. closes. Exposing the cache cadence (TTL["stocks"],
    one hour by default, and skipped entirely while a failed attempt is younger
    than that) turns "has this frozen?" into a visible fact.
    """
    view = dict(screen or {})
    interval = int(config.TTL.get("stocks") or 3600)
    view["refresh_interval_seconds"] = interval
    now = float(now or time.time())
    at = (entry or {}).get("at")
    try:
        at = float(at)
    except (TypeError, ValueError):
        at = None
    if at is not None:
        view["checked_at_utc"] = datetime.fromtimestamp(
            at, timezone.utc).isoformat(timespec="minutes")
        view["next_check_in_seconds"] = max(0, int(at + interval - now))
    return view


def _with_indicative_myr(screen, fx_entry, *, demo=False, now=None):
    """Attach a clearly non-executable USD/MYR reference to stock prices.

    Conversion is suppressed for demo data, failed/expired FX cache entries,
    invalid rates, or reference dates older than the stock screen's freshness
    window. It uses the dashboard's existing ECB/Frankfurter FX cache only.
    """
    view = dict(screen or {})
    candidates = [dict(item) for item in (view.get("candidates") or [])
                  if isinstance(item, dict)]
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    reference = {
        "pair": "USD/MYR",
        "source": "ECB reference rate via Frankfurter",
        "available": False,
        "rate_date": None,
        "fetched_at_utc": None,
        "max_reference_age_days": 5,
    }
    rate = None
    reason = None

    if demo:
        reason = "MYR conversion is disabled for synthetic demo data."
        reference["source"] = "DEMO synthetic FX (not used for conversion)"
    else:
        quotes = (fx_entry or {}).get("data") or []
        quote = next((item for item in quotes
                      if isinstance(item, dict) and
                      str(item.get("name") or "").endswith("(MYR)")), None)
        if not quote:
            reason = "No USD/MYR reference quote is available."
        else:
            reference["rate_date"] = quote.get("date")
            try:
                stamp = float((fx_entry or {}).get("at"))
                reference["fetched_at_utc"] = datetime.fromtimestamp(
                    stamp, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                rate = float(quote.get("close"))
                if not math.isfinite(rate) or rate <= 0:
                    raise ValueError("invalid USD/MYR rate")
                rate_date = date.fromisoformat(str(quote.get("date"))[:10])
                age_days = (now.date() - rate_date).days
                cache_age = now.timestamp() - stamp
                if (fx_entry or {}).get("err"):
                    reason = "The USD/MYR refresh failed; the reference is omitted."
                elif cache_age < 0 or cache_age > config.TTL["fx"]:
                    reason = "The USD/MYR cache is stale; the reference is omitted."
                elif age_days < 0 or age_days > reference["max_reference_age_days"]:
                    reason = f"The USD/MYR reference date is {age_days} days old; conversion is omitted."
                else:
                    reference["available"] = True
                    reference["rate"] = rate
            except (TypeError, ValueError, OverflowError, OSError):
                reason = "The USD/MYR reference rate or timestamp is invalid; conversion is omitted."

    if reason:
        reference["reason"] = reason
    for item in candidates:
        item["close_myr_indicative"] = (
            round(float(item["close"]) * rate, 2)
            if reference["available"] and rate is not None
            and isinstance(item.get("close"), (int, float))
            and math.isfinite(float(item["close"]))
            else None
        )
    view["candidates"] = candidates
    view["currency_reference"] = reference
    return view


def summary():
    curve_sid, curve_title, curve_years = config.FRED_CURVE
    be_sid, be_title = config.FRED_BREAKEVEN
    real_sid, real_title = config.FRED_REAL
    policy_series = {sid: get_data(f"fred:{sid}") for sid, _ in config.FRED_POLICY}
    with LOCK:
        errors = [f"{k}: {v['err']}" for k, v in CACHE.items() if v.get("err")]
    providers = backend.provider_status() if hasattr(backend, "provider_status") else []
    fx_entry = cache_get("fx") or {}
    fx_quotes = fx_entry.get("data") or []
    stock_entry = cache_get("stock_screen") or {}
    stock_error = stock_entry.get("err")
    stock_screen_data = stock_entry.get("data")
    if stock_screen_data is None:
        stock_screen_data = stock_screener.build_screen(
            config.STOCK_UNIVERSE,
            {},
            max_price=config.STOCK_MAX_PRICE,
            min_price=config.STOCK_MIN_PRICE,
            min_avg_dollar_volume=config.STOCK_MIN_AVG_DOLLAR_VOLUME,
            limit=config.STOCK_MAX_RESULTS,
            fetch_error=stock_error,
        )
    else:
        stock_screen_data = stock_screener.current_view(stock_screen_data)
        if stock_error:
            stock_screen_data = dict(stock_screen_data)
            stock_screen_data["refresh_error"] = str(stock_error)[:240]
    stock_screen_data = _with_stock_refresh_meta(stock_screen_data, stock_entry)
    stock_screen_data = _with_indicative_myr(
        stock_screen_data, fx_entry, demo=config.DEMO)
    stock_screen_data["provider_issues"] = _stock_provider_issues(
        providers, stock_screen_data)
    out = {
        "indices": [get_data(f"stooq:{s}") for _, s in config.INDICES],
        "commodities": [get_data(f"stooq:{s}") for _, s in config.COMMODITIES],
        "fx": fx_quotes,
        "cpi": get_data("wb_cpi") or [],
        "gdp": get_data("wb_gdp") or [],
        "policy_pairs": config.FRED_POLICY,
        "policy_series": policy_series,
        "curve": get_data("fred:curve") or [],
        "curve_meta": (curve_sid, curve_title, curve_years),
        "breakeven": get_data("fred:breakeven") or [],
        "breakeven_meta": (be_sid, be_title),
        "real_yield": get_data("fred:real") or [],
        "real_yield_meta": (real_sid, real_title),
        "errors": errors[:6],
        "providers": providers,
        "stock_screen": stock_screen_data,
    }
    out["briefing"] = briefing.build_briefing(out)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "econ/1.0"

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
            elif path == "/summary":
                s = summary()
                page = render.build_summary_page(
                    s,
                    demo=config.DEMO,
                    generated_at=datetime.now(timezone.utc).astimezone(),
                    refresh=config.REFRESH_SECONDS,
                )
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif path == "/stocks":
                s = summary()
                page = render.build_stocks_page(
                    s.get("stock_screen"),
                    demo=config.DEMO,
                    generated_at=datetime.now(timezone.utc).astimezone(),
                    refresh=config.REFRESH_SECONDS,
                )
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif path == "/api/stocks":
                s = summary()
                body = json.dumps(s.get("stock_screen") or {}, default=str, indent=1).encode()
                self._send(200, body, "application/json")
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
