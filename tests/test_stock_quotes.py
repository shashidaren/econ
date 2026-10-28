"""Offline unit tests for the 5-minute ~15m-delayed stock quote overlay."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "dashboard")
if DASH not in sys.path:
    sys.path.insert(0, DASH)

import app  # noqa: E402
import config  # noqa: E402
import demo  # noqa: E402
import render  # noqa: E402
import sources  # noqa: E402


SINA_SAMPLE_PFE_BAC = (
    'var hq_str_gb_pfe="Pfizer Inc.,26.4000,2.33,2026-10-29 22:15:00,'
    '0.6000,25.9500,26.5500,25.8200,32.1000,23.5000,41234567,38000000,'
    '149500000000,1.45,18.20,0.00,0.00,0.00,0.00,5667000000,63,'
    '26.4800,0.30,0.0800,Oct 29 04:10PM EDT,Oct 29 04:00PM EDT,25.8000,850000";\n'
    'var hq_str_gb_bac="Bank of America,42.1500,-1.06,2026-10-29 22:15:02,'
    '-0.4500,42.5000,42.7000,41.9000,48.0000,33.0000,29500000,31000000,'
    '325000000000,3.10,13.60,0.00,0.00,0.00,0.00,7710000000,55,'
    '0.0000,0.00,0.0000,,Oct 29 02:15PM EDT,42.6000,0";\n'
)

NASDAQ_SAMPLE_F = json.dumps({
    "data": {
        "symbol": "F",
        "companyName": "Ford Motor Company Common Stock",
        "marketStatus": "Open",
        "primaryData": {
            "lastSalePrice": "$11.24",
            "netChange": "+0.19",
            "percentageChange": "+1.72%",
            "deltaIndicator": "up",
            "lastTradeTimestamp": "Oct 29, 2026 2:14 PM ET",
            "volume": "34,120,500",
        },
        "secondaryData": {
            "lastSalePrice": "$11.28",
            "netChange": "+0.04",
            "percentageChange": "+0.36%",
        },
        "keyStats": {
            "dayrange": {"label": "High/Low", "value": "11.02 - 11.35"},
            "PreviousClose": {"label": "Previous Close", "value": "$11.05"},
        },
    },
    "status": {"rCode": 200},
})

TENCENT_SAMPLE_T = (
    'v_usT="200~AT&T Inc~T.N~22.45~22.10~22.15~18450200~0~0~22.44~12~'
    '22.46~18~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~2026-10-29 14:15:00~'
    '0.35~1.58~22.60~22.05~22.45/18450200/414206990~18450200~41421~0.26~'
    '17.80~~22.60~22.05~2.49~1609.50~1609.50~AT&T~1.12~25.00~16.50~1.00";\n'
)


class MarketSessionTests(unittest.TestCase):
    def test_us_market_session_states_across_clock_and_weekend(self):
        # Thursday 2026-10-29 (EDT = UTC-4):
        # 12:00 UTC = 08:00 EDT -> pre_market
        pre = sources.us_market_session(datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(pre["state"], "pre_market")
        self.assertFalse(pre["is_open"])

        # 14:30 UTC = 10:30 EDT -> regular
        reg = sources.us_market_session(datetime(2026, 10, 29, 14, 30, tzinfo=timezone.utc))
        self.assertEqual(reg["state"], "regular")
        self.assertTrue(reg["is_open"])

        # 21:00 UTC = 17:00 EDT -> after_hours
        aft = sources.us_market_session(datetime(2026, 10, 29, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(aft["state"], "after_hours")
        self.assertFalse(aft["is_open"])

        # 02:00 UTC = 22:00 EDT Wed -> closed
        closed = sources.us_market_session(datetime(2026, 10, 29, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(closed["state"], "closed")

        # Saturday 2026-10-31 15:00 UTC -> closed (weekend)
        sat = sources.us_market_session(datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(sat["state"], "closed")


class QuoteCascadeTests(unittest.TestCase):
    def setUp(self):
        sources.reset_breakers()

    def tearDown(self):
        sources.reset_breakers()

    def test_sina_batch_parses_quotes_extended_hours_and_punctuated_tickers(self):
        brk_line = (
            'var hq_str_gb_brk$b="Berkshire Hathaway,452.10,0.47,2026-10-29 22:15:00,'
            '2.10,450.50,453.00,449.80,480.00,390.00,3100000,3500000,'
            '980000000000,21.0,21.5,0,0,0,0,2160000000,50,'
            '0.00,0.00,0.00,,Oct 29 02:15PM EDT,450.00,0";\n'
        )
        calls = []

        def fake_get(url, *, timeout=sources.TIMEOUT, headers=None, **kwargs):
            calls.append((url, headers))
            return SINA_SAMPLE_PFE_BAC + brk_line

        # 21:00 UTC = 17:00 EDT (after-hours), so extended fields [21..24] are populated
        now = datetime(2026, 10, 29, 21, 0, tzinfo=timezone.utc)
        with mock.patch.object(sources, "_get", side_effect=fake_get), \
             mock.patch.object(sources, "_pace_host", lambda *a, **k: None):
            out = sources.stock_quotes_batch(["PFE", "BAC", "BRK.B"], now=now)

        self.assertEqual(len(calls), 1)
        self.assertIn("gb_pfe,gb_bac,gb_brk$b", calls[0][0])
        self.assertEqual(calls[0][1].get("Referer"), "https://finance.sina.com.cn/")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["coverage"]["with_quotes"], 3)

        pfe = out["items"]["PFE"]
        self.assertAlmostEqual(pfe["price"], 26.40)
        self.assertAlmostEqual(pfe["change"], 0.60)
        self.assertAlmostEqual(pfe["change_pct"], 2.33)
        self.assertAlmostEqual(pfe["prev_close"], 25.80)
        self.assertAlmostEqual(pfe["day_low"], 25.82)
        self.assertAlmostEqual(pfe["day_high"], 26.55)
        self.assertEqual(pfe["volume"], 41234567)
        # 2026-10-29 22:15:00 UTC+8 -> 14:15:00+00:00
        self.assertEqual(pfe["as_of_utc"], "2026-10-29T14:15:00+00:00")
        self.assertAlmostEqual(pfe["extended_price"], 26.48)
        self.assertEqual(pfe["extended_label"], "After-hours")
        self.assertIn("Sina", pfe["source"])
        self.assertIn("BRK.B", out["items"])
        self.assertAlmostEqual(out["items"]["BRK.B"]["price"], 452.10)

    def test_cascade_falls_through_sina_to_nasdaq_to_tencent(self):
        def fake_get(url, *, timeout=sources.TIMEOUT, headers=None, **kwargs):
            if "hq.sinajs.cn" in url:
                # Sina answers PFE only; F and T come back empty.
                return (
                    SINA_SAMPLE_PFE_BAC.splitlines()[0] + "\n"
                    'var hq_str_gb_f="";\n'
                    'var hq_str_gb_t="";\n'
                )
            if "api.nasdaq.com/api/quote/F/info" in url:
                return NASDAQ_SAMPLE_F
            if "api.nasdaq.com/api/quote/T/info" in url:
                return json.dumps({"data": None, "status": {"rCode": 400}})
            if "qt.gtimg.cn" in url:
                return TENCENT_SAMPLE_T
            raise AssertionError(f"unexpected URL {url}")

        now = datetime(2026, 10, 29, 18, 20, tzinfo=timezone.utc)
        with mock.patch.object(sources, "_get", side_effect=fake_get), \
             mock.patch.object(sources, "_pace_host", lambda *a, **k: None):
            out = sources.stock_quotes_batch(["PFE", "F", "T"], now=now)

        self.assertEqual(out["status"], "ok")
        self.assertEqual(set(out["items"]), {"PFE", "F", "T"})
        self.assertAlmostEqual(out["items"]["F"]["price"], 11.24)
        self.assertAlmostEqual(out["items"]["F"]["day_low"], 11.02)
        self.assertAlmostEqual(out["items"]["F"]["day_high"], 11.35)
        self.assertEqual(out["items"]["F"]["as_of_utc"], "2026-10-29T18:14:00+00:00")
        self.assertIn("Nasdaq", out["items"]["F"]["source"])
        self.assertAlmostEqual(out["items"]["T"]["price"], 22.45)
        self.assertAlmostEqual(out["items"]["T"]["change_pct"], 1.58)
        self.assertIn("Tencent", out["items"]["T"]["source"])
        # Partial symbol misses must NOT open quote breakers
        self.assertEqual(sources.provider_status(), [])

    def test_tencent_none_match_is_symbol_miss_not_breaker_trip(self):
        def fake_get(url, *, timeout=sources.TIMEOUT, headers=None, **kwargs):
            return 'v_pv_none_match="1";\n'

        with mock.patch.object(sources, "_get", side_effect=fake_get), \
             mock.patch.object(sources, "_pace_host", lambda *a, **k: None):
            for _ in range(3):
                with self.assertRaises(sources._SymbolMiss):
                    sources._tencent_us_quotes_batch(["PFE"], session_state="regular")
        self.assertTrue(sources.provider_available(sources.TENCENT_US_QUOTE_KEY))

    def test_quote_breakers_do_not_bleed_into_daily_history_scope(self):
        def fail_get(url, *, timeout=sources.TIMEOUT, headers=None, **kwargs):
            raise sources.HttpStatusError(503, "service unavailable")

        with mock.patch.object(sources, "_get", side_effect=fail_get), \
             mock.patch.object(sources, "_pace_host", lambda *a, **k: None), \
             mock.patch.object(sources, "_yahoo_pace", lambda *a, **k: None), \
             mock.patch.object(sources, "_yahoo_session_crumb", return_value=""):
            out1 = sources.stock_quotes_batch(["PFE"])
            out2 = sources.stock_quotes_batch(["PFE"])

        self.assertEqual(out1["status"], "unavailable")
        self.assertEqual(out2["status"], "unavailable")
        providers = sources.provider_status()
        names = {p["provider"] for p in providers}
        self.assertIn("stock_quote:sina", names)
        self.assertIn("stock_quote:nasdaq", names)
        self.assertIn("stock_quote:tencent", names)
        # Daily-history _stock_provider_issues must ignore stock_quote:* breakers
        self.assertEqual(
            app._stock_provider_issues(providers, {"history_providers": ["sina", "nasdaq"]}),
            [],
        )
        # Quote-scoped helper surfaces them cleanly
        quote_issues = app._stock_quote_provider_issues(providers, out2)
        self.assertTrue(any(p["provider"] == "stock_quote:sina" for p in quote_issues))


class AppQuoteOverlayTests(unittest.TestCase):
    def setUp(self):
        self._saved_cache = dict(app.CACHE)
        self._saved_enabled = config.STOCK_QUOTES_ENABLED
        app.CACHE.clear()
        config.STOCK_QUOTES_ENABLED = True

    def tearDown(self):
        app.CACHE.clear()
        app.CACHE.update(self._saved_cache)
        config.STOCK_QUOTES_ENABLED = self._saved_enabled

    def test_overlay_preserves_daily_close_rank_and_adds_indicative_myr(self):
        now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
        screen = {
            "status": "ok",
            "candidates": [
                {
                    "symbol": "PFE",
                    "name": "Pfizer",
                    "sector": "Healthcare",
                    "close": 25.80,
                    "as_of": "2026-10-28",
                    "ret1_pct": 1.1,
                    "ret7_pct": 3.4,
                    "ret20_pct": 6.2,
                    "ret60_pct": 9.8,
                    "vol20_annual_pct": 21.0,
                    "drawdown60_pct": -4.0,
                    "avg_dollar_volume20": 45_000_000,
                    "rank_score": 1.42,
                    "risk": "Low",
                    "quote_age_days": 1,
                    "source": "Sina daily",
                    "reasons": ["Steady 20d trend"],
                },
                {
                    "symbol": "BAC",
                    "name": "Bank of America",
                    "sector": "Financials",
                    "close": 49.20,
                    "as_of": "2026-10-28",
                    "ret1_pct": 0.8,
                    "ret7_pct": 2.1,
                    "ret20_pct": 5.0,
                    "ret60_pct": 8.0,
                    "vol20_annual_pct": 24.0,
                    "drawdown60_pct": -5.5,
                    "avg_dollar_volume20": 80_000_000,
                    "rank_score": 1.10,
                    "risk": "Low",
                    "quote_age_days": 1,
                    "source": "Sina daily",
                    "reasons": ["Above 20d SMA"],
                },
            ],
        }
        # Intraday BAC jumps above $50 ($51.10, +3.86%) while PFE dips (-0.50%):
        # overlay_only MUST keep PFE #1 and BAC #2 and keep BAC in the list!
        quotes_entry = {
            "at": now.timestamp() - 60,
            "data": {
                "status": "ok",
                "message": "Delayed quotes (~15m) available for 2/2 displayed tickers.",
                "fetched_at_utc": "2026-10-29T14:59:00Z",
                "market_session": {"state": "regular", "is_open": True, "label": "U.S. regular session (09:30–16:00 EDT)"},
                "items": {
                    "PFE": {
                        "symbol": "PFE",
                        "price": 25.67,
                        "change": -0.13,
                        "change_pct": -0.50,
                        "day_low": 25.50,
                        "day_high": 25.95,
                        "as_of_utc": "2026-10-29T14:44:00Z",
                        "delayed": True,
                        "market_status": "regular",
                        "source": "Sina US quote",
                    },
                    "BAC": {
                        "symbol": "BAC",
                        "price": 51.10,
                        "change": 1.90,
                        "change_pct": 3.86,
                        "day_low": 49.40,
                        "day_high": 51.25,
                        "as_of_utc": "2026-10-29T14:44:00Z",
                        "delayed": True,
                        "market_status": "regular",
                        "source": "Sina US quote",
                    },
                },
                "errors": {},
                "quote_providers": ["stock_quote:sina"],
                "coverage": {"symbols": 2, "with_quotes": 2, "providers": ["Sina US quote"]},
            },
        }
        fx_entry = {
            "at": now.timestamp() - 120,
            "data": [{"name": "Malaysian Ringgit (MYR)", "close": 4.35, "date": "2026-10-28"}],
            "err": None,
        }
        merged = app._with_stock_refresh_meta(screen, {"at": now.timestamp() - 900}, now=now.timestamp())
        merged = app._with_stock_quotes(merged, quotes_entry, now=now)
        merged = app._with_indicative_myr(merged, fx_entry, demo=False, now=now)

        self.assertEqual([c["symbol"] for c in merged["candidates"]], ["PFE", "BAC"])
        self.assertAlmostEqual(merged["candidates"][0]["close"], 25.80)
        self.assertAlmostEqual(merged["candidates"][0]["quote"]["price"], 25.67)
        self.assertEqual(merged["candidates"][0]["quote"]["age_seconds"], 960)
        self.assertAlmostEqual(merged["candidates"][0]["quote"]["price_myr_indicative"], round(25.67 * 4.35, 2))
        self.assertAlmostEqual(merged["candidates"][0]["close_myr_indicative"], round(25.80 * 4.35, 2))
        self.assertAlmostEqual(merged["candidates"][1]["quote"]["price"], 51.10)

        page = render.build_stocks_page(merged, demo=False, generated_at=now)
        self.assertIn("$25.67", page)
        self.assertIn("~15m delayed", page)
        self.assertIn("day range $25.50–$25.95", page)
        self.assertIn("Screen close $25.80", page)
        self.assertIn(f"RM {round(25.67 * 4.35, 2):,.2f}", page)
        self.assertIn("Delayed quotes (~15m):", page)
        self.assertIn("Refresh cadence:", page)

    def test_transient_quote_outage_falls_back_to_recent_cached_quotes_without_global_error(self):
        now_ts = time.time()
        app.CACHE["stock_screen"] = {
            "at": now_ts - 60,
            "ttl": 3600,
            "err": None,
            "data": {"status": "ok", "candidates": [{"symbol": "PFE", "close": 25.8}]},
        }
        app.CACHE["stock_quotes"] = {
            "at": now_ts - 310,
            "ttl": 300,
            "err": None,
            "data": {
                "status": "ok",
                "message": "Delayed quotes (~15m) available for 1/1 displayed tickers.",
                "fetched_at_utc": "2026-10-29T14:55:00Z",
                "items": {"PFE": {"symbol": "PFE", "price": 26.10, "delayed": True}},
                "errors": {},
                "quote_providers": ["stock_quote:sina"],
                "coverage": {"symbols": 1, "with_quotes": 1, "providers": ["Sina US quote"]},
            },
        }
        reg_map = {k: (fn, ttl) for k, fn, ttl in app.REGISTRY}
        fn, ttl = reg_map["stock_quotes"]
        unavailable = {
            "status": "unavailable",
            "message": "No delayed quote feed answered.",
            "items": {},
            "errors": {"PFE": "sina: HTTP 503"},
            "quote_providers": ["stock_quote:sina"],
            "coverage": {"symbols": 1, "with_quotes": 0, "providers": []},
        }
        with mock.patch.object(app.backend, "stock_quotes_batch", return_value=unavailable):
            app.fetch_into_cache("stock_quotes", fn, ttl)

        entry = app.cache_get("stock_quotes")
        self.assertIsNone(entry.get("err"))
        self.assertEqual(entry["data"]["status"], "cached")
        self.assertTrue(entry["data"]["served_from_cache"])
        self.assertAlmostEqual(entry["data"]["items"]["PFE"]["price"], 26.10)

    def test_warm_all_runs_stock_screen_before_stock_quotes_on_cold_start(self):
        order = []

        def fake_fetch(key, fn, ttl):
            order.append(key)
            if key == "stock_screen":
                app.CACHE["stock_screen"] = {
                    "at": time.time(),
                    "ttl": ttl,
                    "err": None,
                    "data": {"status": "ok", "candidates": [{"symbol": "PFE", "close": 25.8}]},
                }

        with mock.patch.object(app, "fetch_into_cache", side_effect=fake_fetch), \
             mock.patch.object(app.config, "DEMO", True):
            app.warm_all(force=True)

        self.assertIn("stock_screen", order)
        self.assertIn("stock_quotes", order)
        self.assertLess(order.index("stock_screen"), order.index("stock_quotes"))

    def test_disabled_flag_skips_backend_quote_calls(self):
        config.STOCK_QUOTES_ENABLED = False
        reg_map = {k: (fn, ttl) for k, fn, ttl in app.REGISTRY}
        fn, _ = reg_map["stock_quotes"]
        with mock.patch.object(app.backend, "stock_quotes_batch") as mocked:
            res = fn()
        mocked.assert_not_called()
        self.assertEqual(res["status"], "disabled")


class DemoQuoteTests(unittest.TestCase):
    def test_demo_stock_quotes_are_offline_deterministic_and_suppress_myr(self):
        now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
        out = demo.stock_quotes_batch(["PFE", "F"], now=now)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["demo"])
        self.assertEqual(set(out["items"]), {"PFE", "F"})
        for sym, q in out["items"].items():
            self.assertEqual(q["source"], "DEMO synthetic")
            self.assertTrue(q["demo"])
            self.assertGreater(q["price"], 0)
            self.assertLessEqual(q["day_low"], q["price"])
            self.assertGreaterEqual(q["day_high"], q["price"])

        screen = {
            "status": "ok",
            "candidates": [{"symbol": "PFE", "close": 26.0, "as_of": "2026-10-28"}],
        }
        fx_entry = {
            "at": now.timestamp(),
            "data": [{"name": "Malaysian Ringgit (MYR)", "close": 4.35, "date": "2026-10-28"}],
            "err": None,
        }
        merged = app._with_stock_quotes(screen, {"at": now.timestamp(), "data": out}, now=now)
        merged = app._with_indicative_myr(merged, fx_entry, demo=True, now=now)
        self.assertIsNone(merged["candidates"][0]["close_myr_indicative"])
        self.assertIsNone(merged["candidates"][0]["quote"]["price_myr_indicative"])
        page = render.build_stocks_page(merged, demo=True, generated_at=now)
        self.assertIn("DEMO quote", page)
        self.assertNotIn("RM ", page)


if __name__ == "__main__":
    unittest.main()
