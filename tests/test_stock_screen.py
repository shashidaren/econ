"""Offline tests for the stock watchlist/risk screen and its data adapters."""

import json
import os
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import app  # noqa: E402
import render  # noqa: E402
import sources  # noqa: E402
import stocks  # noqa: E402


def _history(*, start=20.0, growth=0.006, volume=1_000_000, days=80, end=None):
    """Create daily bars with a deterministic proportional drift."""
    end = end or date.today()
    rows = []
    price = start
    for i in range(days):
        day = (end - timedelta(days=days - 1 - i)).isoformat()
        price *= 1.0 + growth
        rows.append((day, price, volume))
    return rows


def _spec(symbol, sector="Technology"):
    return (symbol, f"{symbol} Corp", sector, f"us{symbol}.OQ")


# Wire-format fixtures from the live LXC probes of 2026-10-21 (values are
# illustrative; the envelopes, field names and encodings are the real ones).
SINA_DAILY_K = (
    'var s=([{"d":"2026-10-16","o":"25.400","h":"25.900","l":"25.210","c":"25.750","v":"21483600"},'
    '{"d":"2026-10-15","o":"25.100","h":"25.550","l":"24.980","c":"25.380","v":"19876543"},'
    '{"d":"2026-10-15","o":"25.100","h":"25.550","l":"24.980","c":"25.420","v":"19876543"},'
    '{"d":"2026-10-19","o":"25.800","h":"26.100","l":"25.600","c":"25.990","v":""},'
    '{"d":"bad","o":"1","h":"1","l":"1","c":"1","v":"1"}]);'
)
NASDAQ_HISTORICAL = {
    "data": {"symbol": "PFE", "totalRecords": 3, "tradesTable": {
        "asOf": None,
        "headers": {"date": "Date", "close": "Close/Last", "volume": "Volume",
                    "open": "Open", "high": "High", "low": "Low"},
        "rows": [
            {"date": "10/20/2026", "close": "$26.10", "volume": "21,483,600",
             "open": "$25.80", "high": "$26.25", "low": "$25.61"},
            {"date": "10/19/2026", "close": "$25.99", "volume": "N/A",
             "open": "$25.80", "high": "$26.10", "low": "$25.60"},
            {"date": "10/16/2026", "close": "$25.75", "volume": "19,876,543",
             "open": "$25.40", "high": "$25.90", "low": "$25.21"},
        ]}},
    "message": None,
    "status": {"rCode": 200, "bCodeMessage": None, "developerMessage": None},
}
TENCENT_WAF_501 = (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>501</title></head>"
    "<body><script>window.location.href="
    "\"https://waf.tencent.com/501page.html?u=https://web.ifzq.gtimg.cn/\";"
    "</script></body></html>"
)


def _sina_body(rows):
    """Serialise [(date, close, volume)] as a Sina daily-K JSONP body."""
    items = ",".join(
        json.dumps({"d": d, "o": str(c), "h": str(c), "l": str(c),
                    "c": str(c), "v": str(int(v))})
        for d, c, v in rows)
    return f"var s=([{items}]);"


def _nasdaq_body(rows):
    """Serialise rows as a Nasdaq historical body (newest first, $/commas)."""
    return json.dumps({"data": {"tradesTable": {"rows": [
        {"date": f"{d[5:7]}/{d[8:10]}/{d[:4]}", "close": f"${c:,.2f}",
         "volume": f"{int(v):,}", "open": f"${c:,.2f}",
         "high": f"${c:,.2f}", "low": f"${c:,.2f}"}
        for d, c, v in reversed(rows)]}}})


def _tencent_body(code, rows):
    return json.dumps({"code": 0, "data": {code: {"qfqday": [
        [d, str(c), str(c), str(c), str(c), str(v)] for d, c, v in rows]}}})


class StockScreenTests(unittest.TestCase):
    def test_ranks_every_passer_and_reports_risk(self):
        universe = [_spec(f"T{i}", sector=f"Sector {i}") for i in range(5)]
        data = {
            spec[0]: {"rows": _history(start=15 + i, growth=0.003 + i * 0.001),
                      "source": "test"}
            for i, spec in enumerate(universe)
        }
        result = stocks.build_screen(universe, data)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["eligible_count"], 5)
        self.assertEqual(len(result["candidates"]), 5)
        self.assertEqual(result["max_candidates"], 7)
        self.assertIn("Showing all 5 stock(s)", result["message"])
        self.assertTrue(all(c["risk"] in ("Low", "Medium", "High")
                            for c in result["candidates"]))
        self.assertEqual(result["candidates"][0]["symbol"], "T4")

    def test_display_cap_is_seven_and_eligible_count_is_still_reported(self):
        universe = [_spec(f"T{i}", sector=f"Sector {i}") for i in range(9)]
        data = {
            spec[0]: {"rows": _history(start=15 + i, growth=0.004 + i * 0.0005),
                      "source": "test"}
            for i, spec in enumerate(universe)
        }
        result = stocks.build_screen(universe, data)
        self.assertEqual(result["eligible_count"], 9)
        self.assertEqual(len(result["candidates"]), 7)
        self.assertEqual(result["max_candidates"], 7)
        self.assertIn("9 stock(s) passed", result["message"])
        self.assertIn("top 7", result["message"])
        # Best-ranked name still leads the wider list.
        self.assertEqual(result["candidates"][0]["symbol"], "T8")
        # An explicit smaller cap still applies; an absurd one is clamped so a
        # bad override cannot dump the whole universe onto the tab.
        self.assertEqual(len(stocks.build_screen(universe, data, limit=3)["candidates"]), 3)
        self.assertEqual(len(stocks.build_screen(universe, data, limit=99)["candidates"]), 9)
        self.assertEqual(stocks.build_screen(universe, data, limit=99)["max_candidates"],
                         stocks.MAX_RESULTS_CEILING)

    def test_price_liquidity_freshness_and_trend_filters_are_applied(self):
        universe = [
            _spec("CHEAP"), _spec("OVER"), _spec("THIN"), _spec("STALE"), _spec("DOWN"),
        ]
        today = date.today()
        histories = {
            "CHEAP": _history(start=2.5, growth=0.006),
            "OVER": _history(start=55.0, growth=0.006),
            "THIN": _history(start=20.0, growth=0.006, volume=10),
            "STALE": _history(start=20.0, growth=0.006, end=today - timedelta(days=14)),
        }
        down = _history(start=50.0, growth=-0.006)
        histories["DOWN"] = down
        data = {symbol: {"rows": rows, "source": "test"}
                for symbol, rows in histories.items()}
        result = stocks.build_screen(universe, data)
        self.assertEqual(result["status"], "no_candidates")
        self.assertEqual(result["exclusions"]["price_outside_range"], 2)
        self.assertEqual(result["exclusions"]["low_liquidity_or_no_volume"], 1)
        self.assertEqual(result["exclusions"]["stale_quote"], 1)
        self.assertEqual(result["exclusions"]["weak_trend"], 1)

    def test_risk_buckets_are_backward_looking_and_ordered(self):
        self.assertEqual(stocks._risk_label(20.0, -8.0), "Low")
        self.assertEqual(stocks._risk_label(40.0, -18.0), "Medium")
        self.assertEqual(stocks._risk_label(51.0, -4.0), "High")
        self.assertEqual(stocks._risk_label(15.0, -21.0), "High")

    def test_cached_picks_are_suppressed_after_freshness_window(self):
        universe = [_spec("FRESH")]
        data = {"FRESH": {"rows": _history(), "source": "test"}}
        result = stocks.build_screen(universe, data)
        stale = stocks.current_view(result, as_of=date.today() + timedelta(days=6))
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["candidates"], [])

    def test_cached_screen_is_served_past_the_gate_when_sources_are_blocked(self):
        # A total source outage must degrade, not empty, the tab: with
        # keep_stale the last good screen keeps its picks (honestly aged)
        # and is marked "cached" for the page badge.
        universe = [_spec("FRESH")]
        data = {"FRESH": {"rows": _history(), "source": "test"}}
        result = stocks.build_screen(universe, data)
        self.assertEqual(result["status"], "ok")
        cached = stocks.current_view(
            result, as_of=date.today() + timedelta(days=9), keep_stale=True)
        self.assertEqual(cached["status"], "cached")
        self.assertEqual(len(cached["candidates"]), 1)
        self.assertEqual(cached["candidates"][0]["symbol"], "FRESH")
        self.assertGreater(cached["candidates"][0]["quote_age_days"], 5)
        self.assertEqual(cached["eligible_count"], result["eligible_count"])
        # Within the freshness gate nothing changes.
        still_ok = stocks.current_view(
            result, as_of=date.today() + timedelta(days=2), keep_stale=True)
        self.assertEqual(still_ok["status"], "ok")
        self.assertEqual(len(still_ok["candidates"]), 1)

    def test_cached_screen_page_shows_badge_and_keeps_cards(self):
        universe = [_spec("AAA"), _spec("BBB")]
        data = {
            "AAA": {"rows": _history(start=15, growth=0.006), "source": "Sina daily K"},
            "BBB": {"rows": _history(start=18, growth=0.005), "source": "Sina daily K"},
        }
        screen = stocks.build_screen(universe, data)
        cached = stocks.current_view(
            screen, as_of=date.today() + timedelta(days=9), keep_stale=True)
        cached["refresh_error"] = "RuntimeError: no usable US stock histories"
        cached["served_from_cache"] = True
        cached["sources_blocked"] = True
        page = render.build_stocks_page(cached, demo=False)
        self.assertIn("cached · sources blocked · data as of", page)
        self.assertIn(str(screen["data_date"]), page)
        self.assertEqual(page.count('class="stock-card"'), 2)
        self.assertIn("2/2 tickers with usable history", page)
        self.assertNotIn("0/2 tickers", page)
        self.assertIn("Last refresh failed; showing the last successful screen.", page)

    def test_coverage_line_survives_attached_news(self):
        # Regression: the headlines block used to shadow the `coverage`
        # status string with the news coverage dict, printing a dict repr
        # ("{'symbols': 7, ...}") instead of "N/32 tickers with usable
        # history" whenever headlines were active.
        universe = [_spec("AAA"), _spec("BBB")]
        data = {
            "AAA": {"rows": _history(start=15, growth=0.006), "source": "Sina daily K"},
            "BBB": {"rows": _history(start=18, growth=0.005), "source": "Sina daily K"},
        }
        screen = stocks.build_screen(universe, data)
        screen["news"] = {
            "status": "ok",
            "message": "Recent headlines found for 2 of 2 displayed tickers.",
            "items": {}, "errors": {},
            "coverage": {"symbols": 2, "with_headlines": 2, "headlines": 4,
                         "providers": ["Google News RSS"]},
        }
        page = render.build_stocks_page(screen, demo=False)
        self.assertIn("2/2 tickers with usable history", page)
        self.assertNotIn("with_headlines", page)
        self.assertIn("Google News RSS", page)

    def test_summary_serves_last_good_screen_with_blocked_flags(self):
        universe_rows = _history()
        screen = stocks.build_screen(
            [_spec("FRESH")],
            {"FRESH": {"rows": universe_rows, "source": "test"}})
        # Age the screen past the freshness gate, then fail the refresh.
        screen = dict(screen)
        old = (date.today() - timedelta(days=9)).isoformat()
        screen["data_date"] = old
        screen["candidates"] = [dict(c, as_of=old) for c in screen["candidates"]]
        saved = dict(app.CACHE)
        try:
            app.CACHE.clear()
            app.CACHE["stock_screen"] = {
                "data": screen, "at": time.time(), "ttl": 3600,
                "err": "RuntimeError: no usable US stock histories (blocked by WAF)",
            }
            view = app.summary()["stock_screen"]
        finally:
            app.CACHE.clear()
            app.CACHE.update(saved)
        self.assertTrue(view["served_from_cache"])
        self.assertTrue(view["sources_blocked"])
        self.assertEqual(view["status"], "cached")
        self.assertEqual(len(view["candidates"]), 1)
        self.assertEqual(view["data_date"], old)

    def test_empty_screen_exposes_unavailable_data_instead_of_fake_picks(self):
        result = stocks.build_screen([_spec("MISSING")], {}, fetch_error="feed blocked")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["candidates"], [])
        self.assertIn("feed blocked", result["fetch_error"])

    def test_unavailable_page_shows_fetch_failure_and_no_candidates(self):
        screen = stocks.build_screen(
            [_spec("MISSING")], {}, fetch_error="Tencent timeout; Yahoo HTTP 429")
        page = render.build_stocks_page(screen, demo=False)
        self.assertEqual(screen["status"], "unavailable")
        self.assertEqual(screen["candidates"], [])
        self.assertIn("Fresh stock history is unavailable", page)
        self.assertIn("History fetch detail", page)
        self.assertIn("Tencent timeout; Yahoo HTTP 429", page)
        self.assertEqual(page.count('class="stock-card"'), 0)

    def test_usable_history_coverage_requires_minimum_sessions(self):
        universe = [_spec("FULL"), _spec("SHORT")]
        data = {
            "FULL": {"rows": _history(days=80), "source": "test"},
            "SHORT": {"rows": _history(days=20), "source": "test"},
        }
        result = stocks.build_screen(universe, data)
        self.assertEqual(result["history_count"], 2)
        self.assertEqual(result["usable_history_count"], 1)
        self.assertEqual(result["exclusions"]["insufficient_history"], 1)

    def test_stocks_page_shows_every_passer_and_escapes_text(self):
        cards = []
        for i in range(5):
            cards.append({
                "symbol": f"T{i}", "name": "Company <script>", "sector": "Technology",
                "close": 20 + i, "risk": "Medium", "ret5_pct": 1.0,
                "ret20_pct": 2.0, "ret60_pct": 5.0, "vol20_annual_pct": 24.0,
                "drawdown60_pct": -4.0, "avg_dollar_volume20": 10_000_000,
                "rank_score": 2.5, "as_of": date.today().isoformat(),
                "source": "test", "quote_age_days": 0, "reasons": ["above 20-day average"],
            })
        page = render.build_stocks_page({
            "status": "ok", "message": "5 eligible", "universe_size": 5,
            "quote_count": 5, "eligible_count": 5, "candidates": cards,
            "max_candidates": 7,
            "data_date": date.today().isoformat(), "source": "test",
            "filters": {"price_min": 5, "price_max_exclusive": 50,
                        "min_avg_dollar_volume": 5_000_000},
            "exclusions": {},
        }, demo=True)
        self.assertEqual(page.count('class="stock-card"'), 5)
        self.assertIn("up to 7 names", page)
        self.assertIn("Stocks", page)
        self.assertNotIn("<script>", page)
        self.assertIn("DEMO MODE — synthetic stock history", page)
        self.assertNotIn("RM 20.00", page)
        self.assertIn("MYR conversion is disabled for synthetic demo data", page)

    def test_stocks_page_honours_the_display_cap_when_more_pass(self):
        def card(i):
            return {
                "symbol": f"T{i}", "name": f"Company {i}", "sector": "Technology",
                "close": 20 + i, "risk": "Medium", "ret5_pct": 1.0,
                "ret20_pct": 2.0, "ret60_pct": 5.0, "vol20_annual_pct": 24.0,
                "drawdown60_pct": -4.0, "avg_dollar_volume20": 10_000_000,
                "rank_score": 2.5, "as_of": date.today().isoformat(),
                "source": "test", "quote_age_days": 0, "reasons": [],
            }
        screen = {
            "status": "ok", "message": "9 eligible", "universe_size": 32,
            "quote_count": 32, "eligible_count": 9,
            "candidates": [card(i) for i in range(9)], "max_candidates": 7,
            "data_date": date.today().isoformat(), "source": "test",
            "filters": {"price_min": 5, "price_max_exclusive": 50,
                        "min_avg_dollar_volume": 5_000_000},
            "exclusions": {},
        }
        page = render.build_stocks_page(screen, demo=False)
        self.assertEqual(page.count('class="stock-card"'), 7)
        self.assertIn("up to 7 names", page)
        self.assertIn('class="stock-symbol">T6<', page)
        self.assertNotIn('class="stock-symbol">T8<', page)
        # Lowering the configured cap narrows the rendered list too.
        screen["max_candidates"] = 3
        self.assertEqual(
            render.build_stocks_page(screen, demo=False).count('class="stock-card"'), 3)

    def test_stocks_page_surfaces_partial_history_and_provider_failures(self):
        page = render.build_stocks_page({
            "status": "ok", "message": "1 eligible", "universe_size": 2,
            "quote_count": 1, "usable_history_count": 1, "history_count": 1,
            "candidates": [], "data_date": "2026-10-12", "source": "Tencent K-line",
            "history_errors": {"count": 1, "samples": {
                "MISS": "Tencent: unavailable; Yahoo: empty; Stooq: timeout",
            }},
            "provider_issues": [{
                "provider": "tencent_kline", "state": "cooling", "retry_in": 120,
                "reason": "connection timeout",
            }],
        }, demo=False)
        self.assertIn("1/2 tickers with usable history", page)
        self.assertIn("MISS", page)
        self.assertIn("tencent_kline: cooling down", page)
        self.assertIn("retry in about 2 min", page)
        self.assertIn("connection timeout", page)

    def test_stocks_page_shows_dual_market_times_and_caveated_myr(self):
        now = datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc)
        page = render.build_stocks_page({
            "status": "ok", "message": "1 eligible", "universe_size": 1,
            "quote_count": 1, "usable_history_count": 1, "history_count": 1,
            "candidates": [{
                "symbol": "TEST", "name": "Test Corp", "sector": "Technology",
                "close": 20.0, "close_myr_indicative": 90.0, "risk": "Low",
                "ret5_pct": 1.0, "ret20_pct": 2.0, "ret60_pct": 5.0,
                "vol20_annual_pct": 20.0, "drawdown60_pct": -4.0,
                "avg_dollar_volume20": 10_000_000, "rank_score": 2.5,
                "as_of": "2026-10-12", "source": "test", "quote_age_days": 1,
                "reasons": [],
            }],
            "data_date": "2026-10-12", "source": "test",
            "generated_at": "2026-10-13T12:00:00Z",
            "filters": {"price_min": 5, "price_max_exclusive": 50,
                        "min_avg_dollar_volume": 5_000_000, "minimum_sessions": 61},
            "currency_reference": {
                "available": True, "pair": "USD/MYR", "rate": 4.5,
                "source": "ECB reference rate via Frankfurter",
                "rate_date": "2026-10-12", "fetched_at_utc": "2026-10-13T12:00:00Z",
            },
        }, demo=False, generated_at=now)
        self.assertIn("Kuala Lumpur: 2026-10-13 20:30 MYT", page)
        self.assertIn("U.S. Eastern: 2026-10-13 08:30 EDT", page)
        self.assertIn("U.S. market session date, Eastern Time", page)
        self.assertIn("RM 90.00", page)
        self.assertIn("ECB reference rate via Frankfurter", page)
        self.assertIn("reference date 2026-10-12", page)
        self.assertIn("2026-10-13 20:00 MYT", page)
        self.assertIn("Not an executable or all-in price", page)
        self.assertIn("stamp-duty, spread and FX conversion costs are excluded", page)
        self.assertIn("https://www.moomoo.com/my/support/topic9_136", page)
        self.assertIn("https://www.moomoo.com/my/support/topic9_193", page)


class StockRefreshCadenceTests(unittest.TestCase):
    """The screen is a daily-close list; the page must say how often it re-checks."""

    def _screen(self, age_seconds, **extra):
        now = time.time()
        entry = {"data": {"candidates": []}, "at": now - age_seconds,
                 "ttl": app.config.TTL["stocks"], "err": None}
        screen = {"status": "ok", "message": "1 eligible", "candidates": [],
                  "data_date": date.today().isoformat(), "source": "Tencent K-line"}
        screen.update(extra)
        return app._with_stock_refresh_meta(screen, entry, now=now)

    def test_screen_reports_interval_and_next_check_time(self):
        screen = self._screen(900)
        self.assertEqual(screen["refresh_interval_seconds"], app.config.TTL["stocks"])
        self.assertEqual(screen["refresh_interval_seconds"], 3600)
        self.assertEqual(screen["next_check_in_seconds"], 2700)
        self.assertTrue(screen["checked_at_utc"].endswith("+00:00"))
        page = render.build_stocks_page(screen, demo=False)
        self.assertIn("Refresh cadence", page)
        self.assertIn("at most every 60 min", page)
        self.assertIn("next re-check in about 45 min", page)
        self.assertIn("not intraday", page)

    def test_overdue_entry_reports_next_check_due_now(self):
        screen = self._screen(7200)
        self.assertEqual(screen["next_check_in_seconds"], 0)
        self.assertIn("next re-check due now",
                      render.build_stocks_page(screen, demo=False))

    def test_page_without_cache_metadata_omits_the_cadence_line(self):
        page = render.build_stocks_page(
            {"status": "ok", "message": "1 eligible", "candidates": []}, demo=False)
        self.assertNotIn("Refresh cadence", page)


class StockCurrencyTests(unittest.TestCase):
    def test_fresh_ecb_reference_adds_only_an_indicative_myr_value(self):
        now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        screen = {"candidates": [{"symbol": "TEST", "close": 20.0}]}
        fx_entry = {
            "data": [{"name": "Malaysian Ringgit (MYR)", "close": 4.5,
                      "date": "2026-10-12", "source": "ECB"}],
            "at": now.timestamp() - 60,
            "ttl": app.config.TTL["fx"],
            "err": None,
        }
        result = app._with_indicative_myr(screen, fx_entry, now=now)
        self.assertTrue(result["currency_reference"]["available"])
        self.assertEqual(result["currency_reference"]["pair"], "USD/MYR")
        self.assertEqual(result["currency_reference"]["source"],
                         "ECB reference rate via Frankfurter")
        self.assertEqual(result["candidates"][0]["close_myr_indicative"], 90.0)
        self.assertNotIn("close_myr_indicative", screen["candidates"][0])

    def test_stale_or_demo_fx_is_omitted(self):
        now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        fx_entry = {
            "data": [{"name": "Malaysian Ringgit (MYR)", "close": 4.5,
                      "date": "2026-10-07", "source": "ECB"}],
            "at": now.timestamp() - 60,
            "err": None,
        }
        stale = app._with_indicative_myr(
            {"candidates": [{"close": 20.0}]}, fx_entry, now=now)
        self.assertFalse(stale["currency_reference"]["available"])
        self.assertIsNone(stale["candidates"][0]["close_myr_indicative"])
        self.assertIn("6 days old", stale["currency_reference"]["reason"])

        demo = app._with_indicative_myr(
            {"candidates": [{"close": 20.0}]}, fx_entry, demo=True, now=now)
        self.assertFalse(demo["currency_reference"]["available"])
        self.assertIn("synthetic demo data", demo["currency_reference"]["reason"])
        self.assertIsNone(demo["candidates"][0]["close_myr_indicative"])


class StockDataAdapterTests(unittest.TestCase):
    def setUp(self):
        sources.reset_breakers()
        sources._HOST_LAST_AT.clear()
        self._pace = sources.STOCK_PACE_SECONDS
        sources.STOCK_PACE_SECONDS = 0  # pacing is covered by its own test
        self._cookie_setting = sources.YAHOO_USE_COOKIE
        sources.YAHOO_USE_COOKIE = False
        sources._YAHOO_LAST_AT = 0.0

    def tearDown(self):
        sources.STOCK_PACE_SECONDS = self._pace
        sources.YAHOO_USE_COOKIE = self._cookie_setting
        sources.reset_breakers()
        sources._HOST_LAST_AT.clear()

    def test_tencent_kline_parser_reads_daily_close_and_volume(self):
        payload = {
            "code": 0,
            "data": {"usTEST.OQ": {"qfqday": [
                ["2026-10-08", "18.0", "19.25", "19.5", "17.9", "1234567"],
                ["2026-10-09", "19.2", "20.0", "20.4", "19.0", "2345678"],
            ]}},
        }
        rows = sources._parse_tencent_kline_payload(payload, "usTEST.OQ")
        self.assertEqual(rows, [
            ("2026-10-08", 19.25, 1234567.0),
            ("2026-10-09", 20.0, 2345678.0),
        ])

    def test_tencent_kline_parser_accepts_callback_and_handles_empty_volume(self):
        body = 'kline_dayqfq({"data":{"usTEST.N":{"day":[["2026-10-09","1","2","2","1",""]]}}});'
        rows = sources._parse_tencent_kline_payload(body, "usTEST.N")
        self.assertEqual(rows, [("2026-10-09", 2.0, None)])

    def test_sina_daily_k_parser_sorts_dedupes_and_handles_missing_volume(self):
        rows = sources._parse_sina_daily_k_payload(SINA_DAILY_K)
        self.assertEqual(rows, [
            ("2026-10-15", 25.42, 19876543.0),   # duplicate date: last wins
            ("2026-10-16", 25.75, 21483600.0),
            ("2026-10-19", 25.99, None),          # empty volume field
        ])

    def test_sina_daily_k_parser_accepts_wrapper_variants_and_rejects_junk(self):
        inner = '[{"d":"2026-10-20","c":"26.10","v":"100"}]'
        expected = [("2026-10-20", 26.10, 100.0)]
        self.assertEqual(sources._parse_sina_daily_k_payload(f"var s=({inner});"), expected)
        self.assertEqual(sources._parse_sina_daily_k_payload(f"var s={inner};"), expected)
        self.assertEqual(sources._parse_sina_daily_k_payload(inner), expected)
        self.assertEqual(sources._parse_sina_daily_k_payload("var s=(null);"), [])
        self.assertEqual(sources._parse_sina_daily_k_payload(""), [])
        self.assertEqual(sources._parse_sina_daily_k_payload(TENCENT_WAF_501), [])
        self.assertEqual(sources._parse_sina_daily_k_payload({"d": "x"}), [])

    def test_nasdaq_parser_strips_dollar_and_commas_and_sorts_ascending(self):
        expected = [
            ("2026-10-16", 25.75, 19876543.0),
            ("2026-10-19", 25.99, None),          # "N/A" volume
            ("2026-10-20", 26.10, 21483600.0),
        ]
        self.assertEqual(
            sources._parse_nasdaq_historical_payload(NASDAQ_HISTORICAL), expected)
        self.assertEqual(
            sources._parse_nasdaq_historical_payload(json.dumps(NASDAQ_HISTORICAL)),
            expected)

    def test_nasdaq_parser_handles_null_data_and_junk(self):
        self.assertEqual(sources._parse_nasdaq_historical_payload(
            {"data": None, "status": {"rCode": 400}}), [])
        self.assertEqual(sources._parse_nasdaq_historical_payload("not json"), [])
        self.assertEqual(sources._parse_nasdaq_historical_payload([]), [])

    def test_waf_block_body_detection(self):
        self.assertTrue(sources._is_waf_block_body(TENCENT_WAF_501))
        self.assertTrue(sources._is_waf_block_body("  \n<HTML><body>blocked</body>"))
        self.assertTrue(sources._is_waf_block_body("<!doctype html>challenge"))
        self.assertTrue(sources._is_waf_block_body(
            "anything containing waf.tencent.com in the middle"))
        self.assertFalse(sources._is_waf_block_body(SINA_DAILY_K))
        self.assertFalse(sources._is_waf_block_body('{"data":{"tradesTable":{}}}'))
        self.assertFalse(sources._is_waf_block_body("kline_dayqfq({\"data\":{}})"))
        self.assertFalse(sources._is_waf_block_body(""))
        self.assertFalse(sources._is_waf_block_body(None))

    def test_tencent_kline_url_param_survives_percent_encoding(self):
        # Live evidence: %2C-encoded and literal commas both drew the WAF 501,
        # so encoding is not the outage cause; the URL builder must simply
        # round-trip the documented param shape.
        import urllib.parse
        url = sources._tencent_kline_url(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", "usPFE.N", days=130)
        self.assertIn("%2C", url)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(params["param"], ["usPFE.N,day,,,130,qfq"])

    def test_nasdaq_url_spans_enough_calendar_days_for_130_sessions(self):
        import urllib.parse
        from datetime import date as _date
        url = sources._nasdaq_historical_url("PFE", days=130)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        span = (_date.fromisoformat(params["todate"][0])
                - _date.fromisoformat(params["fromdate"][0])).days
        self.assertGreaterEqual(span, 200)  # >=61 sessions with big headroom
        self.assertGreaterEqual(int(params["limit"][0]), 200)
        self.assertIn("/api/quote/PFE/historical", url)

    def test_yahoo_chart_parser_keeps_volume_for_screener(self):
        result = {
            "timestamp": [1791417600, 1791504000],
            "indicators": {"quote": [{"close": [10.0, 11.0], "volume": [100, None]}]},
        }
        rows = sources._parse_chart_result_with_volume(result)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1:], (10.0, 100.0))
        self.assertIsNone(rows[1][2])

    def test_stock_history_cascade_falls_from_sina_to_nasdaq(self):
        rows = _history(days=70)

        def fake_sina(symbol, days=130, budget=None):
            if symbol == "AAA":
                return rows
            raise RuntimeError("Sina symbol unavailable")

        with mock.patch.object(sources, "_sina_us_daily_k_history", side_effect=fake_sina), \
                mock.patch.object(sources, "_nasdaq_us_stock_history", return_value=rows) as nasdaq, \
                mock.patch.object(sources, "_tencent_us_stock_history") as tencent, \
                mock.patch.object(sources, "_yahoo_spark_batch") as yahoo, \
                mock.patch.object(sources, "_stooq_us_stock_history") as stooq:
            out = sources.stock_history_batch(
                ["AAA", "BBB"], {"AAA": "usAAA.OQ", "BBB": "usBBB.N"}, days=70)
        self.assertEqual(out["AAA"]["source"], "Sina daily K")
        self.assertEqual(out["BBB"]["source"], "Nasdaq")
        self.assertEqual(out["_meta"]["history_providers"], ["sina", "nasdaq"])
        nasdaq.assert_called_once_with("BBB", days=70, budget=mock.ANY)
        tencent.assert_not_called()
        yahoo.assert_not_called()
        stooq.assert_not_called()

    def test_stock_history_uses_yahoo_batch_after_per_symbol_misses(self):
        rows = _history(days=70)
        miss = RuntimeError("symbol unavailable")
        with mock.patch.object(sources, "_sina_us_daily_k_history", side_effect=miss), \
                mock.patch.object(sources, "_nasdaq_us_stock_history", side_effect=miss), \
                mock.patch.object(sources, "_tencent_us_stock_history", side_effect=miss), \
                mock.patch.object(sources, "_yahoo_spark_batch", return_value={"BBB": rows}) as yahoo, \
                mock.patch.object(sources, "_stooq_us_stock_history") as stooq:
            out = sources.stock_history_batch(
                ["BBB"], {"BBB": "usBBB.N"}, days=70)
        self.assertEqual(out["BBB"]["source"], "Yahoo Finance")
        self.assertEqual(out["_meta"]["history_providers"],
                         ["sina", "nasdaq", "tencent_kline", "yahoo"])
        yahoo.assert_called_once_with(["BBB"], days=70, include_volume=True)
        stooq.assert_not_called()

    def test_partial_provider_failures_are_reported_for_unresolved_tickers(self):
        rows = _history(days=70)

        def fake_sina(symbol, days=130, budget=None):
            if symbol == "GOOD":
                return rows
            raise RuntimeError("Sina symbol unavailable")

        with mock.patch.object(sources, "_sina_us_daily_k_history", side_effect=fake_sina), \
                mock.patch.object(sources, "_nasdaq_us_stock_history",
                                  side_effect=RuntimeError("Nasdaq symbol unavailable")), \
                mock.patch.object(sources, "_tencent_us_stock_history",
                                  side_effect=RuntimeError("Tencent symbol unavailable")), \
                mock.patch.object(sources, "provider_available", return_value=True), \
                mock.patch.object(sources, "_yahoo_spark_batch", return_value={}), \
                mock.patch.object(sources, "_stooq_us_stock_history",
                                  side_effect=RuntimeError("Stooq offline")):
            out = sources.stock_history_batch(
                ["GOOD", "MISS"],
                {"GOOD": "usGOOD.OQ", "MISS": "usMISS.OQ"}, days=70)

        diagnostics = out["_meta"]["history_errors"]
        self.assertEqual(diagnostics["count"], 1)
        self.assertIn("MISS", diagnostics["samples"])
        for label in ("Sina daily K", "Nasdaq", "Tencent K-line", "Yahoo Finance", "Stooq"):
            self.assertIn(label, diagnostics["samples"]["MISS"])
        screen = stocks.build_screen([_spec("GOOD"), _spec("MISS")], out)
        self.assertEqual(screen["history_errors"]["count"], 1)

    def test_sina_down_nasdaq_serves_the_cascade(self):
        # The required end-to-end shape: transport faked at sources._get, the
        # real cascade, parsers, pacing and breakers all run.
        rows = _history(days=70)

        def fake_get(url, timeout=None, **kwargs):
            if "stock.finance.sina.com.cn" in url:
                raise RuntimeError("curl: (28) Failed to connect: Timeout was reached")
            if "api.nasdaq.com" in url:
                return _nasdaq_body(rows)
            raise AssertionError(f"unexpected URL {url}")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            out = sources.stock_history_batch(
                ["PFE"], {"PFE": "usPFE.N"}, days=70)

        self.assertEqual(out["PFE"]["source"], "Nasdaq")
        self.assertEqual(out["PFE"]["rows"][-1][0], rows[-1][0])
        self.assertEqual(out["_meta"]["history_providers"], ["sina", "nasdaq"])
        # Sina's host breaker cools after the connect timeout; Nasdaq's does not.
        self.assertFalse(sources.provider_available(sources.SINA_KLINE_KEY))
        self.assertTrue(sources.provider_available(sources.NASDAQ_KEY))

    def test_waf_501_cools_only_the_blocked_tencent_host(self):
        # Per-host isolation: web.ifzq.gtimg.cn is WAF-blocked (HTTP 501 with
        # the captured body), proxy.finance.qq.com still serves, and neither
        # Sina host breaker is touched by Tencent's block.
        rows = _history(days=70)

        def fake_get(url, timeout=None, **kwargs):
            if "stock.finance.sina.com.cn" in url:
                return "var s=([]);"  # answered, no K-lines: a symbol miss
            if "api.nasdaq.com" in url:
                return json.dumps({"data": None, "status": {"rCode": 400}})
            if "web.ifzq.gtimg.cn" in url:
                raise sources.HttpStatusError(501, TENCENT_WAF_501)
            if "proxy.finance.qq.com" in url:
                return _tencent_body("usPFE.N", rows)
            raise AssertionError(f"unexpected URL {url}")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            out = sources.stock_history_batch(
                ["PFE"], {"PFE": "usPFE.N"}, days=70)

        self.assertEqual(out["PFE"]["source"], "Tencent K-line")
        self.assertFalse(sources.provider_available("tencent_kline:web.ifzq.gtimg.cn"))
        self.assertTrue(sources.provider_available("tencent_kline:proxy.finance.qq.com"))
        self.assertTrue(sources.provider_available(sources.SINA_KLINE_KEY))
        self.assertTrue(sources.provider_available(sources.NASDAQ_KEY))
        self.assertTrue(sources.provider_available("sina"))  # hq index quotes
        status = {s["provider"]: s for s in sources.provider_status()}
        self.assertIn("blocked by WAF",
                      status["tencent_kline:web.ifzq.gtimg.cn"]["reason"])
        self.assertEqual(status["tencent_kline:web.ifzq.gtimg.cn"]["state"], "cooling")

    def test_waf_html_body_on_http_200_is_also_a_host_block(self):
        # Some WAFs serve the block page with HTTP 200; the body check must
        # catch it before the parser ever sees HTML.
        def fake_get(url, timeout=None, **kwargs):
            if "stock.finance.sina.com.cn" in url:
                return "var s=([]);"
            if "api.nasdaq.com" in url:
                return json.dumps({"data": None})
            if "ifzq.gtimg.cn" in url or "proxy.finance.qq.com" in url:
                return TENCENT_WAF_501
            if "finance.yahoo.com" in url:
                raise RuntimeError("curl: (22) error: 429 HTTP 429")
            if "stooq." in url:
                raise RuntimeError("curl: (28) Failed to connect: Timeout was reached")
            raise AssertionError(f"unexpected URL {url}")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            with self.assertRaises(RuntimeError) as ctx:
                sources.stock_history_batch(["PFE"], {"PFE": "usPFE.N"}, days=70)
        self.assertIn("blocked by WAF", str(ctx.exception))
        self.assertFalse(sources.provider_available("tencent_kline:web.ifzq.gtimg.cn"))
        self.assertFalse(sources.provider_available("tencent_kline:proxy.finance.qq.com"))
        # Symbol misses on Sina/Nasdaq never trip their breakers.
        self.assertTrue(sources.provider_available(sources.SINA_KLINE_KEY))
        self.assertTrue(sources.provider_available(sources.NASDAQ_KEY))

    def test_request_budget_caps_a_refresh(self):
        rows = _history(days=70)
        calls = []

        def fake_get(url, timeout=None, **kwargs):
            calls.append(url)
            return _sina_body(rows)

        with mock.patch.object(sources, "STOCK_REFRESH_REQUEST_BUDGET", 1), \
                mock.patch.object(sources, "_get", side_effect=fake_get):
            out = sources.stock_history_batch(
                ["AAA", "BBB"], {"AAA": "usAAA.OQ", "BBB": "usBBB.N"}, days=70)
        self.assertEqual(len(calls), 1)  # the whole refresh spent one request
        self.assertEqual(len([k for k in out if k != "_meta"]), 1)
        self.assertEqual(out["_meta"]["history_requests"], 1)
        samples = out["_meta"]["history_errors"]["samples"]
        self.assertEqual(len(samples), 1)
        self.assertIn("request budget exhausted", next(iter(samples.values())))

    def test_pace_host_enforces_a_minimum_gap_per_host(self):
        sources._HOST_LAST_AT["pace-test"] = time.time()
        started = time.monotonic()
        with mock.patch.object(sources.random, "uniform", return_value=0.0):
            sources._pace_host("pace-test", interval=0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.04)
        # A different host key is not delayed by the first host's timestamp.
        started = time.monotonic()
        sources._pace_host("pace-test-other", interval=0.05)
        self.assertLess(time.monotonic() - started, 0.04)
        # Non-positive interval disables pacing entirely.
        started = time.monotonic()
        sources._pace_host("pace-test", interval=0)
        self.assertLess(time.monotonic() - started, 0.04)


class StockProviderScopeTests(unittest.TestCase):
    """Stock diagnostics must reflect only the providers its refresh used."""

    def setUp(self):
        sources.reset_breakers()
        sources._HOST_LAST_AT.clear()
        self._pace = sources.STOCK_PACE_SECONDS
        sources.STOCK_PACE_SECONDS = 0
        self._cookie_setting = sources.YAHOO_USE_COOKIE
        sources.YAHOO_USE_COOKIE = False
        sources._YAHOO_LAST_AT = 0.0

    def tearDown(self):
        sources.STOCK_PACE_SECONDS = self._pace
        sources.YAHOO_USE_COOKIE = self._cookie_setting
        sources.reset_breakers()
        sources._HOST_LAST_AT.clear()

    def test_full_sina_coverage_ignores_unrelated_global_breakers(self):
        rows = _history(days=70)
        # Board-wide trouble unrelated to stocks: index quotes failing on Stooq
        # and spark charts failing on Yahoo open their shared breakers.
        sources.note_provider_failure(
            "stooq:stooq.com", "curl: (28) Failed to connect: Timeout was reached",
            unreachable=True)
        sources.note_provider_failure(
            "yahoo", "curl: (22) error: 429 HTTP 429", rate_limited=True)

        with mock.patch.object(sources, "_sina_us_daily_k_history",
                               side_effect=lambda symbol, days=130, budget=None: rows) as sina, \
                mock.patch.object(sources, "_nasdaq_us_stock_history") as nasdaq, \
                mock.patch.object(sources, "_tencent_us_stock_history") as tencent, \
                mock.patch.object(sources, "_yahoo_spark_batch") as yahoo, \
                mock.patch.object(sources, "_stooq_us_stock_history") as stooq:
            out = sources.stock_history_batch(
                ["AAA", "BBB"], {"AAA": "usAAA.OQ", "BBB": "usBBB.N"}, days=70)

        self.assertEqual(set(out) - {"_meta"}, {"AAA", "BBB"})
        self.assertTrue(all(out[s]["source"] == "Sina daily K"
                            for s in ("AAA", "BBB")))
        self.assertEqual(out["_meta"]["history_providers"], ["sina"])
        self.assertNotIn("history_errors", out["_meta"])
        self.assertEqual(sina.call_count, 2)
        nasdaq.assert_not_called()
        tencent.assert_not_called()
        yahoo.assert_not_called()
        stooq.assert_not_called()

        screen = stocks.build_screen([_spec("AAA"), _spec("BBB")], out)
        self.assertEqual(screen["status"], "ok")
        self.assertEqual(screen["usable_history_count"], 2)
        self.assertEqual(screen["history_errors"]["count"], 0)
        self.assertEqual(
            app._stock_provider_issues(sources.provider_status(), screen), [])
        # Without the refresh scope the shared breakers would have leaked in.
        self.assertEqual(len(app._stock_provider_issues(sources.provider_status())), 2)

        screen["provider_issues"] = app._stock_provider_issues(
            sources.provider_status(), screen)
        page = render.build_stocks_page(screen, demo=False)
        self.assertIn('class="stock-status ok"', page)
        self.assertNotIn('class="stock-status warn"', page)
        self.assertIn("2/2 tickers with usable history", page)
        self.assertNotIn("Data notes", page)
        self.assertNotIn("cooling down", page)
        self.assertNotIn("HTTP 429", page)

    def test_genuine_stock_source_failures_keep_stock_scoped_warnings(self):
        rows = _history(days=70)

        def fake_get(url, timeout=None, **kwargs):
            if "stock.finance.sina.com.cn" in url:
                if "symbol=GOOD" in url:
                    return _sina_body(rows)
                # A per-symbol mapping miss: no K-lines, and by design no
                # breaker trip for the shared Sina host.
                return "var s=([]);"
            if "api.nasdaq.com" in url:
                raise RuntimeError(
                    "curl: (28) Failed to connect: Timeout was reached")
            if "ifzq.gtimg.cn" in url or "proxy.finance.qq.com" in url:
                raise sources.HttpStatusError(501, TENCENT_WAF_501)
            if "finance.yahoo.com" in url:
                raise RuntimeError("curl: (22) error: 429 HTTP 429")
            if "stooq." in url:
                raise RuntimeError(
                    "curl: (28) Failed to connect: Timeout was reached")
            raise AssertionError(f"unexpected URL {url}")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            out = sources.stock_history_batch(
                ["GOOD", "MISS"], {"GOOD": "usGOOD.OQ", "MISS": "usMISS.OQ"},
                days=70)

        self.assertEqual(out["GOOD"]["source"], "Sina daily K")
        self.assertEqual(out["_meta"]["history_providers"],
                         ["sina", "nasdaq", "tencent_kline", "yahoo", "stooq"])
        diagnostics = out["_meta"]["history_errors"]
        self.assertEqual(diagnostics["count"], 1)
        for label in ("Sina daily K", "Nasdaq", "Tencent K-line",
                      "blocked by WAF", "Yahoo Finance", "Stooq"):
            self.assertIn(label, diagnostics["samples"]["MISS"])

        # Per-host isolation, live: Tencent's WAF block cools only its hosts;
        # the Sina daily-K host that served GOOD is untouched, as is the
        # unrelated hq.sinajs.cn index-quote breaker ("sina").
        self.assertTrue(sources.provider_available(sources.SINA_KLINE_KEY))
        self.assertTrue(sources.provider_available("sina"))
        self.assertFalse(sources.provider_available("tencent_kline:web.ifzq.gtimg.cn"))
        self.assertFalse(sources.provider_available("tencent_kline:proxy.finance.qq.com"))

        # Nasdaq, Tencent, Yahoo and Stooq were genuinely attempted for this
        # refresh, so their breaker states are in scope; the global list
        # alone is not trusted.
        screen = stocks.build_screen([_spec("GOOD"), _spec("MISS")], out)
        self.assertEqual(screen["history_errors"]["count"], 1)
        issues = app._stock_provider_issues(sources.provider_status(), screen)
        names = {issue["provider"] for issue in issues}
        self.assertIn("yahoo", names)
        self.assertIn("nasdaq:api.nasdaq.com", names)
        self.assertIn("tencent_kline:web.ifzq.gtimg.cn", names)
        self.assertTrue(any(name.startswith("stooq") for name in names))
        self.assertNotIn("sina", names)

        screen["provider_issues"] = issues
        page = render.build_stocks_page(screen, demo=False)
        self.assertIn('class="stock-status warn"', page)
        self.assertIn("Data notes", page)
        self.assertIn("1 ticker(s) lacked usable history", page)
        self.assertIn("MISS", page)
        self.assertIn("blocked by WAF", page)
        # The page prints only the first four provider lines (longest
        # cooldown first): Nasdaq's 30 min line outranks Yahoo's 15 min one,
        # but Yahoo's 429 still shows in the per-ticker sample above.
        self.assertIn("nasdaq:api.nasdaq.com: cooling down", page)
        self.assertIn("HTTP 429", page)
        self.assertTrue("stooq:stooq.com" in page or "stooq:stooq.pl" in page)

    def test_scope_filter_keeps_family_view_without_refresh_metadata(self):
        providers = [
            {"provider": "stooq:stooq.com", "state": "cooling", "retry_in": 60,
             "reason": "Timeout was reached"},
            {"provider": "yahoo", "state": "cooling", "retry_in": 30,
             "reason": "HTTP 429"},
            {"provider": "tencent_kline:web.ifzq.gtimg.cn", "state": "cooling",
             "retry_in": 900, "reason": "blocked by WAF (HTTP 501)"},
            {"provider": "sina:stock.finance.sina.com.cn", "state": "cooling",
             "retry_in": 300, "reason": "Timeout was reached"},
            {"provider": "nasdaq:api.nasdaq.com", "state": "degraded", "retry_in": 0,
             "reason": "timeout"},
            {"provider": "sina", "state": "cooling", "retry_in": 60,
             "reason": "hq index quotes, not stock history"},
            {"provider": "fred", "state": "cooling", "retry_in": 60,
             "reason": "no data"},
        ]
        stock_keys = ["stooq:stooq.com", "yahoo",
                      "tencent_kline:web.ifzq.gtimg.cn",
                      "sina:stock.finance.sina.com.cn", "nasdaq:api.nasdaq.com"]
        # Family filter: the five stock-history families only. The bare
        # "sina" key is the hq.sinajs.cn index-quote breaker and stays out.
        self.assertEqual(
            [p["provider"] for p in app._stock_provider_issues(providers)],
            stock_keys)
        # Scoped to a Sina-only refresh: every other family drops out.
        self.assertEqual(
            app._stock_provider_issues(providers, {"history_providers": ["sina"]}),
            [dict(providers[3])])
        # The scoped tencent_kline:<host> key maps back to its family.
        self.assertEqual(
            app._stock_provider_issues(providers, {"history_providers": ["tencent_kline"]}),
            [dict(providers[2])])
        scoped = app._stock_provider_issues(
            providers, {"history_providers": ["tencent_kline", "stooq"]})
        self.assertEqual([p["provider"] for p in scoped],
                         ["stooq:stooq.com", "tencent_kline:web.ifzq.gtimg.cn"])
        # Screens without refresh scope (demo/synthetic data) keep the
        # family-wide view instead of hiding everything.
        self.assertEqual(len(app._stock_provider_issues(providers, {})), 5)
        self.assertEqual(
            len(app._stock_provider_issues(providers, {"history_providers": None})), 5)

    def test_warning_styling_is_preserved_for_stale_and_failed_refreshes(self):
        ok_screen = stocks.build_screen(
            [_spec("FRESH")],
            {"FRESH": {"rows": _history(), "source": "Tencent K-line"}})
        self.assertEqual(ok_screen["status"], "ok")

        # A failed refresh keeps the panel warned while last-good data shows.
        failed = dict(ok_screen)
        failed["refresh_error"] = "no usable US stock histories (all providers down)"
        page = render.build_stocks_page(failed, demo=False)
        self.assertIn('class="stock-status warn"', page)
        self.assertIn("Last refresh failed", page)

        # Stale cached history is flagged, not quietly green.
        stale = stocks.current_view(ok_screen, as_of=date.today() + timedelta(days=6))
        page = render.build_stocks_page(stale, demo=False)
        self.assertIn('class="stock-status bad"', page)
        self.assertIn("stale", page)

        # Total source failure: bad panel with the fetch detail preserved.
        dead = stocks.build_screen(
            [_spec("MISS")], {},
            fetch_error="Tencent timeout; Yahoo HTTP 429")
        page = render.build_stocks_page(dead, demo=False)
        self.assertEqual(dead["status"], "unavailable")
        self.assertIn('class="stock-status bad"', page)
        self.assertIn("History fetch detail", page)
        self.assertIn("Tencent timeout; Yahoo HTTP 429", page)


class DataStatusTests(unittest.TestCase):
    def test_market_quote_mapping_counts_as_one_card(self):
        self.assertEqual(app._data_item_count({"name": "S&P 500", "close": 5200}), 1)

    def test_section_status_reports_partial_and_delay(self):
        keys = app._SECTION_CACHE_KEYS["indices"]
        now = 1_800_000_000.0
        fixtures = {
            keys[0]: {"data": [{"name": "S&P 500", "stale": False}],
                     "at": now - 120, "ttl": 600, "err": None},
            keys[1]: {"data": [{"name": "Nasdaq", "stale": False}],
                     "at": now - 60, "ttl": 600, "err": "temporary failure"},
        }
        with mock.patch.object(app, "cache_get", side_effect=lambda key: fixtures.get(key)):
            status = app.data_status(now=now)["indices"]
        self.assertEqual(status["status"], "delayed")
        self.assertEqual(status["available"], 2)
        self.assertEqual(status["expected"], len(keys))
        self.assertEqual(status["cache_age_seconds"], 120)
        self.assertEqual(status["issues"], [keys[1]])

    def test_summary_exposes_freshness_contract(self):
        saved = dict(app.CACHE)
        try:
            app.CACHE.clear()
            summary = app.summary()
            self.assertIn("generated_at_utc", summary)
            self.assertIn("data_status", summary)
            self.assertIn("indices", summary["data_status"])
        finally:
            app.CACHE.clear()
            app.CACHE.update(saved)


if __name__ == "__main__":
    unittest.main()
