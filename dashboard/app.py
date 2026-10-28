#!/usr/bin/env python3
"""econ dashboard — a self-hosted, glanceable world-economy board.

Python stdlib only (no pip deps). Runs as a systemd service on the LXC.

  ECON_PORT=8080     listen port (default 8080)
  ECON_HOST=0.0.0.0  bind address
  ECON_DEMO=1        serve bundled sample data (no network) for previews
  ECON_REFRESH=300   background cache refresh cadence (seconds)

Endpoints:  /             the dashboard
            /summary      prose summary
            /economies    Economy 101 (US vs Malaysia explainer)
            /stocks       short-term stock screen
            /api/summary  data as JSON
            /api/economies Economy 101 view as JSON
            /api/stocks   stock screen as JSON
            /api/status   compact freshness probe
            /healthz      liveness probe
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
import economy
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

def economy_history():
    """Annual World Bank series for the Economy 101 tab (US + Malaysia).

    One payload, four indicators. A single failing indicator is recorded as an
    error string and does not blank the others; only a complete miss raises, so
    the cache keeps the last good series.
    """
    countries = {str(c["code"]).upper(): {"name": c["name"], "indicators": {}}
                 for c in config.ECON_COUNTRIES}
    errors = {}
    for meta in config.ECON_INDICATORS:
        key, indicator = meta["key"], meta["indicator"]
        try:
            rows = backend.worldbank_history(
                list(countries), indicator, years=config.ECON_HISTORY_YEARS)
        except Exception as exc:  # noqa: BLE001 — one indicator must not blank the tab
            errors[key] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        for code, payload in rows.items():
            if code not in countries:
                continue
            countries[code]["name"] = payload.get("name") or countries[code]["name"]
            countries[code]["indicators"][key] = payload.get("series") or []
    if not any(entry["indicators"] for entry in countries.values()):
        raise RuntimeError("worldbank: no annual series for the Economy tab")
    return {
        "countries": countries,
        "errors": errors,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _displayed_stock_symbols() -> list[str]:
    """Ticker symbols currently shown by the cached stock screen (up to 7)."""
    entry = cache_get("stock_screen") or {}
    screen = entry.get("data")
    if not isinstance(screen, dict):
        return []
    return [
        str(c.get("symbol")).strip().upper()
        for c in (screen.get("candidates") or [])
        if isinstance(c, dict) and str(c.get("symbol") or "").strip()
    ]


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
        screen = stock_screener.build_screen(
            config.STOCK_UNIVERSE,
            histories,
            max_price=config.STOCK_MAX_PRICE,
            min_price=config.STOCK_MIN_PRICE,
            min_avg_dollar_volume=config.STOCK_MIN_AVG_DOLLAR_VOLUME,
            limit=config.STOCK_MAX_RESULTS,
        )
        # Headlines are context only and must never break the screen: they are
        # fetched for the displayed (already ranked and capped) candidates and
        # any failure becomes a visible status string, not an exception.
        if not config.STOCK_NEWS_ENABLED:
            screen["news"] = {
                "status": "disabled",
                "message": "Stock headlines are disabled (ECON_STOCK_NEWS=0).",
                "items": {}, "errors": {},
            }
        else:
            try:
                screen["news"] = backend.fetch_news(
                    screen.get("candidates") or [],
                    days=config.STOCK_NEWS_WINDOW_DAYS,
                    per_symbol=config.STOCK_NEWS_PER_SYMBOL,
                )
            except Exception as exc:  # noqa: BLE001 — never fail the screen
                screen["news"] = {
                    "status": "unavailable",
                    "message": (f"Headline fetch failed: "
                                f"{type(exc).__name__}: {exc}")[:240],
                    "items": {}, "errors": {},
                }
        return screen
    reg.append(("stock_screen", stock_screen_data, config.TTL["stocks"]))

    # 6. Delayed / intraday stock quotes (~15-min delayed keyless overlay for
    # the displayed stock picks, refreshed every 5 min on the board cycle).
    def stock_quotes_data():
        if not config.STOCK_QUOTES_ENABLED:
            return {
                "status": "disabled",
                "message": "Delayed stock quotes are disabled (ECON_STOCK_QUOTES=0).",
                "items": {},
                "errors": {},
                "quote_providers": [],
                "coverage": {"symbols": 0, "with_quotes": 0, "providers": []},
            }
        symbols = _displayed_stock_symbols()
        if not symbols or not hasattr(backend, "stock_quotes_batch"):
            return {
                "status": "waiting",
                "message": (
                    "Waiting for screened stock picks before looking up "
                    "delayed quotes."
                ),
                "items": {},
                "errors": {},
                "quote_providers": [],
                "coverage": {"symbols": 0, "with_quotes": 0, "providers": []},
            }
        try:
            result = backend.stock_quotes_batch(symbols)
        except Exception as exc:  # noqa: BLE001 — never crash the refresher
            result = {
                "status": "unavailable",
                "message": (
                    f"Delayed stock quote lookup failed: "
                    f"{type(exc).__name__}: {exc}"
                )[:240],
                "items": {},
                "errors": {},
                "quote_providers": [],
                "coverage": {"symbols": len(symbols), "with_quotes": 0,
                             "providers": []},
            }
        # On a transient 5-minute quote outage, keep recently fetched quotes
        # (< 1 h old) badged as cached rather than dropping straight to daily
        # closes; older cached quotes expire cleanly back to daily closes.
        if result.get("status") == "unavailable":
            prev_entry = cache_get("stock_quotes") or {}
            prev = prev_entry.get("data")
            prev_at = prev_entry.get("at") or 0
            if (isinstance(prev, dict) and prev.get("items")
                    and (time.time() - float(prev_at)) < 3600):
                degraded = dict(prev)
                degraded["status"] = "cached"
                degraded["served_from_cache"] = True
                degraded["refresh_error"] = str(
                    result.get("message") or "quote refresh failed"
                )[:240]
                degraded["quote_providers"] = (
                    result.get("quote_providers")
                    or prev.get("quote_providers")
                    or []
                )
                return degraded
        return result
    reg.append(("stock_quotes", stock_quotes_data,
                config.TTL.get("stock_quotes", 300)))

    # 7. Economy 101 tab: annual World Bank series for the US and Malaysia.
    # Same provider as the macro panel; one cached payload on a daily TTL.
    reg.append(("economy_history", economy_history, config.TTL.get(
        "economy", 24 * 60 * 60)))

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

    # If `stock_screen` is refreshing in this cycle, run `stock_quotes` after
    # the worker pool finishes so cold boots and hourly screen updates quote
    # the newly selected watchlist picks rather than an empty or stale list.
    screen_refreshing = any(k == "stock_screen" for k, _, _ in todo)
    if screen_refreshing:
        pool_todo = [item for item in todo if item[0] != "stock_quotes"]
        deferred_quotes = [item for item in todo if item[0] == "stock_quotes"]
    else:
        pool_todo = todo
        deferred_quotes = []

    workers = 1 if config.DEMO else 4
    if pool_todo:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="warm") as pool:
            futures = [pool.submit(fetch_into_cache, key, fn, ttl)
                       for key, fn, ttl in pool_todo]
            for fut in futures:
                try:
                    fut.result()
                except Exception:
                    traceback.print_exc()

    if screen_refreshing and not deferred_quotes and config.STOCK_QUOTES_ENABLED:
        # Screen refreshed while quotes were still inside their 5-min TTL:
        # re-fetch quotes only if a newly ranked ticker lacks a quote.
        existing = ((cache_get("stock_quotes") or {}).get("data") or {}).get("items") or {}
        needed = _displayed_stock_symbols()
        if needed and any(sym not in existing for sym in needed):
            deferred_quotes = [item for item in REGISTRY if item[0] == "stock_quotes"]

    for key, fn, ttl in deferred_quotes:
        try:
            fetch_into_cache(key, fn, ttl)
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


# Group cache entries by the visible sections they feed. The oldest contributing
# cache age is used so a section never looks fresher than its stalest input.
_SECTION_CACHE_KEYS = {
    "indices": [f"stooq:{symbol}" for _, symbol in config.INDICES],
    "commodities": [f"stooq:{symbol}" for _, symbol in config.COMMODITIES],
    "fx": ["fx"],
    "macro": ["wb_cpi", "wb_gdp"],
    "rates": ([f"fred:{sid}" for sid, _ in config.FRED_POLICY]
              + ["fred:curve", "fred:breakeven", "fred:real"]),
}
_SECTION_EXPECTED = {
    "indices": len(config.INDICES),
    "commodities": len(config.COMMODITIES),
    "fx": len(config.FX_CURRENCIES),
    "macro": len(config.WB_COUNTRIES) * 2,
    "rates": len(_SECTION_CACHE_KEYS["rates"]),
}


def _data_item_count(data):
    if isinstance(data, dict):
        # A market quote is itself a mapping; count it as one card, not one
        # item per field. Other mapping payloads are counted by their entries.
        return 1 if "close" in data else len(data)
    if isinstance(data, (list, tuple)):
        return len(data)
    return int(data is not None)


def data_status(*, now=None):
    """Per-section cache freshness and coverage, for UI and diagnostics."""
    now = float(now or time.time())
    result = {}
    for section, keys in _SECTION_CACHE_KEYS.items():
        entries = [(key, cache_get(key)) for key in keys]
        populated = [(key, entry) for key, entry in entries
                     if entry and _data_item_count(entry.get("data")) > 0]
        ages = [max(0.0, now - float(entry.get("at", now)))
                for _, entry in populated]
        stale_observation = any(
            isinstance(item, dict) and item.get("stale")
            for _, entry in populated
            for item in (entry.get("data") if isinstance(entry.get("data"), list) else [])
        )
        errors = [key for key, entry in entries if entry and entry.get("err")]
        expired = any(
            age >= float((entry or {}).get("ttl", 600))
            for age, (_, entry) in zip(ages, populated)
        )
        if section == "rates":
            available_count = len(populated)
        else:
            available_count = sum(_data_item_count(entry.get("data"))
                                  for _, entry in populated)
        expected_count = _SECTION_EXPECTED[section]
        if errors:
            state = "delayed"
        elif available_count == 0:
            state = "unavailable"
        elif available_count < expected_count:
            state = "partial"
        elif expired or stale_observation:
            state = "stale"
        else:
            state = "fresh"
        oldest_at = min(
            (float(entry.get("at", 0)) for _, entry in populated),
            default=None,
        )
        result[section] = {
            "status": state,
            "label": {
                "fresh": "Fresh", "partial": "Partial", "delayed": "Delayed",
                "stale": "Stale", "unavailable": "Awaiting data",
            }[state],
            "available": available_count,
            "expected": expected_count,
            "cache_age_seconds": int(max(ages)) if ages else None,
            "last_checked_at_utc": (
                datetime.fromtimestamp(oldest_at, timezone.utc).isoformat(timespec="seconds")
                if oldest_at else None
            ),
            "issues": errors,
        }
    return result


def _stock_breaker_family(name):
    """Map a breaker key to the stock-history cascade stage that shares it.

    Stock-history breakers are host-scoped (`sina:<host>`, `nasdaq:<host>`,
    `tencent_kline:<host>`, `stooq:<host>`), so families are matched by
    prefix. The bare `sina` key is the hq.sinajs.cn *index-quote* breaker,
    not the daily-K host, and must not leak into Stocks-tab diagnostics —
    hence the colon in the `sina:` prefix.
    """
    if name.startswith("sina:"):
        return "sina"
    if name.startswith("nasdaq"):
        return "nasdaq"
    if name.startswith("tencent_kline"):
        return "tencent_kline"
    if name == "yahoo":
        return "yahoo"
    if name.startswith("stooq"):
        return "stooq"
    return None


def _stock_provider_issues(providers, screen=None):
    """Keep stock-tab diagnostics scoped to its history-provider families.

    When `screen` records the provider families its latest history refresh
    actually needed (`history_providers`), only those are reported. The
    `yahoo` and `stooq:*` breakers are shared with non-stock features (index
    quotes, spark charts), so their outages must not degrade a fully covered
    refresh that never touched them. Screens without that scope keep the
    wider view.
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


def _stock_quote_provider_issues(providers, quotes=None):
    """Scope quote-provider breaker warnings to stages the quote refresh touched."""
    scope = (quotes or {}).get("quote_providers")
    scope = {str(name) for name in scope} if scope is not None else None
    out = []
    for item in providers or []:
        name = str(item.get("provider") or "")
        if not (name.startswith("stock_quote:") or name == "yahoo"):
            continue
        if scope is not None and name not in scope:
            continue
        if name == "yahoo" and scope is None:
            continue
        out.append(dict(item))
    return out


def _with_stock_quotes(screen, quotes_entry, *, providers=None, now=None):
    """Attach ~15-min-delayed quotes to displayed candidates without re-ranking.

    Daily-screen fields (`close`, `as_of`, `ret1_pct`, `rank_score`, `risk`,
    etc.) remain intact so the filter and rank order stay anchored to settled
    daily closes. Each candidate gets a `quote` dict when a fresh quote is
    available, or `None` when quotes are disabled/warming/unavailable.
    """
    view = dict(screen or {})
    interval = int(config.TTL.get("stock_quotes") or 300)
    now_dt = now or datetime.now(timezone.utc)
    if isinstance(now_dt, (int, float)):
        now_dt = datetime.fromtimestamp(float(now_dt), timezone.utc)
    elif now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_dt = now_dt.astimezone(timezone.utc)
    now_ts = now_dt.timestamp()

    raw_quotes = (quotes_entry or {}).get("data")
    if not config.STOCK_QUOTES_ENABLED:
        quotes_view = {
            "status": "disabled",
            "message": "Delayed stock quotes are disabled (ECON_STOCK_QUOTES=0).",
            "items": {},
            "errors": {},
            "refresh_interval_seconds": interval,
            "provider_issues": [],
        }
    elif not isinstance(raw_quotes, dict):
        quotes_view = {
            "status": "waiting",
            "message": "Waiting for the 5-minute delayed-quote refresh.",
            "items": {},
            "errors": {},
            "refresh_interval_seconds": interval,
            "provider_issues": [],
        }
    else:
        quotes_view = dict(raw_quotes)
        quotes_view["refresh_interval_seconds"] = interval
        at = (quotes_entry or {}).get("at")
        try:
            at_f = float(at)
        except (TypeError, ValueError):
            at_f = None
        if at_f is not None:
            quotes_view["checked_at_utc"] = datetime.fromtimestamp(
                at_f, timezone.utc
            ).isoformat(timespec="minutes")
            quotes_view["next_check_in_seconds"] = max(
                0, int(at_f + interval - now_ts)
            )
            # Disk-restored quotes older than 6 hours without a fresh refresh
            # are suppressed so stale quotes never masquerade as intraday data.
            if (now_ts - at_f) > 6 * 3600 and not quotes_view.get("demo"):
                quotes_view["status"] = "stale"
                quotes_view["message"] = (
                    "Cached delayed quotes are older than 6 hours; showing "
                    "latest daily closes until the next quote refresh."
                )
        quotes_view["provider_issues"] = _stock_quote_provider_issues(
            providers, quotes_view
        )

    items_map = (
        quotes_view.get("items")
        if quotes_view.get("status") in ("ok", "partial", "cached")
        and isinstance(quotes_view.get("items"), dict)
        else {}
    )
    candidates = []
    for cand in view.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        item = dict(cand)
        sym = str(item.get("symbol") or "").strip().upper()
        raw_q = items_map.get(sym)
        attached = None
        if isinstance(raw_q, dict):
            try:
                price_val = float(raw_q.get("price"))
            except (TypeError, ValueError):
                price_val = None
            if price_val is not None and math.isfinite(price_val) and price_val > 0:
                q_copy = dict(raw_q)
                as_of_utc = q_copy.get("as_of_utc")
                age_sec = None
                if as_of_utc:
                    try:
                        raw_iso = str(as_of_utc)
                        if raw_iso.endswith("Z"):
                            raw_iso = raw_iso[:-1] + "+00:00"
                        q_dt = datetime.fromisoformat(raw_iso)
                        if q_dt.tzinfo is None:
                            q_dt = q_dt.replace(tzinfo=timezone.utc)
                        age_sec = max(0, int((now_dt - q_dt).total_seconds()))
                    except (TypeError, ValueError, OverflowError):
                        age_sec = None
                q_copy["age_seconds"] = age_sec
                # Guard against an upstream returning an ancient suspended print.
                max_age_sec = 7 * 86400
                if age_sec is None or age_sec <= max_age_sec:
                    attached = q_copy
        item["quote"] = attached
        candidates.append(item)

    view["candidates"] = candidates
    view["quotes"] = quotes_view
    return view


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
        if isinstance(item.get("quote"), dict):
            q_copy = dict(item["quote"])
            q_price = q_copy.get("price")
            q_copy["price_myr_indicative"] = (
                round(float(q_price) * rate, 2)
                if reference["available"] and rate is not None
                and isinstance(q_price, (int, float))
                and math.isfinite(float(q_price))
                else None
            )
            item["quote"] = q_copy
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
    quotes_entry = cache_get("stock_quotes") or {}
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
        stock_screen_data["news"] = {
            "status": "waiting",
            "message": ("Waiting for the first stock refresh before looking up "
                        "headlines."),
            "items": {}, "errors": {},
            "window_days": config.STOCK_NEWS_WINDOW_DAYS,
        }
    else:
        # While fresh stock history is failing, serve the last good screen
        # (memory or disk cache) instead of blanking the tab once its quotes
        # age past the freshness gate; the page badges it as cached with the
        # data-as-of date. A total source outage must degrade, not empty.
        stock_screen_data = stock_screener.current_view(
            stock_screen_data, keep_stale=bool(stock_error))
        if stock_error:
            stock_screen_data = dict(stock_screen_data)
            stock_screen_data["refresh_error"] = str(stock_error)[:240]
            stock_screen_data["served_from_cache"] = True
            stock_screen_data["sources_blocked"] = True
    stock_screen_data = _with_stock_refresh_meta(stock_screen_data, stock_entry)
    stock_screen_data = _with_stock_quotes(
        stock_screen_data, quotes_entry, providers=providers)
    stock_screen_data = _with_indicative_myr(
        stock_screen_data, fx_entry, demo=config.DEMO)
    stock_screen_data["provider_issues"] = _stock_provider_issues(
        providers, stock_screen_data)
    out = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_status": data_status(),
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
        "economy_history": get_data("economy_history") or {},
    }
    out["briefing"] = briefing.build_briefing(out)
    out["economy"] = economy.build_economy(out)
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
            elif path == "/economies":
                s = summary()
                page = render.build_economy_page(
                    s.get("economy") or {},
                    demo=config.DEMO,
                    generated_at=datetime.now(timezone.utc).astimezone(),
                    refresh=config.REFRESH_SECONDS,
                )
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif path == "/api/economies":
                s = summary()
                body = json.dumps(s.get("economy") or {}, default=str,
                                  indent=1).encode()
                self._send(200, body, "application/json")
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
            elif path == "/api/status":
                s = summary()
                stock = s.get("stock_screen") or {}
                quotes = stock.get("quotes") or {}
                payload = {
                    "generated_at_utc": s.get("generated_at_utc"),
                    "sections": s.get("data_status") or {},
                    "source_errors": s.get("errors") or [],
                    "provider_issues": s.get("providers") or [],
                    "stock_screen": {
                        "status": stock.get("status"),
                        "data_date": stock.get("data_date"),
                        "checked_at_utc": stock.get("checked_at_utc"),
                        "next_check_in_seconds": stock.get("next_check_in_seconds"),
                        "served_from_cache": bool(stock.get("served_from_cache")),
                    },
                    "stock_quotes": {
                        "status": quotes.get("status"),
                        "market_session": (quotes.get("market_session") or {}).get("state"),
                        "checked_at_utc": quotes.get("checked_at_utc"),
                        "next_check_in_seconds": quotes.get("next_check_in_seconds"),
                        "served_from_cache": bool(quotes.get("served_from_cache")),
                        "coverage": quotes.get("coverage") or {},
                    },
                }
                body = json.dumps(payload, default=str, indent=1).encode()
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
