"""Offline drawdown calculations, multi-source OHLC/EOD cascade, live quotes, cache and HTTP tests."""
import json
import sys
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import app
import config
import demo
import drawdown
import render
import sources


class DrawdownTest(unittest.TestCase):
    def setUp(self):
        sources.reset_breakers()

    def test_peak_and_latest_are_different_and_rows_sorted(self):
        item = drawdown.from_history("test", [
            ("2026-11-19", 90), ("2020-01-01", 50),
            ("2026-11-18", 100), ("2026-11-17", None)], "feed",
            today=date(2026, 11, 20))
        self.assertEqual(item["current"], 90)
        self.assertEqual(item["peak"], 100)
        self.assertEqual(item["close_peak"], 100)
        self.assertAlmostEqual(item["drawdown_pct"], -10)
        self.assertAlmostEqual(item["close_drawdown_pct"], -10)
        self.assertEqual(item["peak_date"], "2026-11-18")
        self.assertEqual(item["history_start"], "2020-01-01")
        # Close-only rows must not invent an intraday high.
        self.assertIsNone(item["intraday_peak"])
        self.assertIsNone(item["intraday_drawdown_pct"])

    def test_ohlc_history_computes_both_intraday_and_closing_drawdowns(self):
        item = drawdown.from_history("S&P 500", [
            ("2026-11-17", 99.0, 105.0),
            ("2026-11-18", 100.0, 101.0),
            ("2026-11-20", 90.0, 92.0),
        ], "Sina", today=date(2026, 11, 20))
        self.assertEqual(item["current"], 90.0)
        self.assertEqual(item["close_peak"], 100.0)
        self.assertEqual(item["close_peak_date"], "2026-11-18")
        self.assertAlmostEqual(item["close_drawdown_pct"], -10.0)
        self.assertEqual(item["intraday_peak"], 105.0)
        self.assertEqual(item["intraday_peak_date"], "2026-11-17")
        self.assertAlmostEqual(item["intraday_drawdown_pct"], (90.0 / 105.0 - 1.0) * 100.0)

    def test_high_today_zero_and_invalid_rows_ignored(self):
        item = drawdown.from_history("test", [("2026-11-20", 110),
            ("2026-11-19", 100), ("2027-01-01", 500),
            ("2026-11-18", float("nan")), ("oops", 800)], "feed",
            today=date(2026, 11, 20))
        self.assertEqual(item["drawdown_pct"], 0)
        self.assertEqual(item["peak"], 110)

    def test_stale_and_missing_rejected(self):
        for rows in ([], [("2020-01-01", 200)], [("2026-11-20", -1)]):
            with self.assertRaises(ValueError):
                drawdown.from_history("test", rows, "feed", today=date(2026, 11, 20))

    def test_apply_quote_regular_vs_settled_and_stale_guard(self):
        base = drawdown.from_history("S&P 500", [
            ("2026-11-18", 6000.0, 6050.0),
            ("2026-11-19", 5900.0, 5950.0),
        ], "Sina", today=date(2026, 11, 20))
        now_utc = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)

        # Open regular session trading above prior close peak and setting a new intraday high:
        live = drawdown.apply_quote(base, {
            "price": 6030.0,
            "prev_close": 5900.0,
            "day_high": 6080.0,
            "trade_date": "2026-11-20",
            "as_of_utc": "2026-11-20T15:55:00+00:00",
            "market_status": "regular",
            "source": "Sina US index quote",
        }, now=now_utc)
        self.assertTrue(live["current_is_live_quote"])
        self.assertEqual(live["current"], 6030.0)
        self.assertEqual(live["close_peak"], 6000.0)
        self.assertEqual(live["close_drawdown_pct"], 0.0)
        self.assertAlmostEqual(live["above_close_peak_pct"], 0.5)
        self.assertEqual(live["intraday_peak"], 6080.0)
        self.assertEqual(live["intraday_peak_date"], "2026-11-20")
        self.assertAlmostEqual(live["intraday_drawdown_pct"], (6030.0 / 6080.0 - 1.0) * 100.0)

        # Settled session close updates close_peak when above prior close high:
        settled = drawdown.apply_quote(base, {
            "price": 6040.0,
            "prev_close": 5900.0,
            "day_high": 6080.0,
            "trade_date": "2026-11-20",
            "as_of_utc": "2026-11-20T21:15:00+00:00",
            "market_status": "closed",
            "source": "Sina US index quote",
        }, now=datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc))
        self.assertEqual(settled["close_peak"], 6040.0)
        self.assertEqual(settled["close_peak_date"], "2026-11-20")
        self.assertEqual(settled["close_drawdown_pct"], 0.0)

        # Quote older than daily_close_date must be ignored:
        older = drawdown.apply_quote(base, {
            "price": 5500.0,
            "trade_date": "2026-11-17",
            "as_of_utc": "2026-11-17T21:00:00+00:00",
            "market_status": "closed",
        }, now=now_utc)
        self.assertFalse(older["current_is_live_quote"])
        self.assertEqual(older["current"], 5900.0)

        # Close-only history uses 52-week high only when peak is within 52 weeks:
        close_only_recent = drawdown.from_history("Dow", [
            ("2026-10-01", 43000.0),
            ("2026-11-19", 42000.0),
        ], "FRED", today=date(2026, 11, 20))
        with_52w = drawdown.apply_quote(close_only_recent, {
            "price": 42500.0,
            "week52_high": 43500.0,
            "trade_date": "2026-11-20",
            "as_of_utc": "2026-11-20T16:00:00+00:00",
            "market_status": "regular",
        }, now=now_utc)
        self.assertEqual(with_52w["intraday_peak"], 43500.0)
        self.assertIn("52-week", with_52w["intraday_peak_note"])

        close_only_old_peak = drawdown.from_history("Dow", [
            ("2022-01-04", 45000.0),
            ("2026-11-19", 42000.0),
        ], "FRED", today=date(2026, 11, 20))
        old_peak_q = drawdown.apply_quote(close_only_old_peak, {
            "price": 42500.0,
            "week52_high": 43500.0,
            "trade_date": "2026-11-20",
            "as_of_utc": "2026-11-20T16:00:00+00:00",
            "market_status": "regular",
        }, now=now_utc)
        self.assertIsNone(old_peak_q["intraday_peak"])

    def test_yahoo_period1_daily_history_url_and_rejects_monthly_granularity(self):
        ts = int(datetime.now(timezone.utc).timestamp())
        payload = {"chart": {"result": [{
            "meta": {"dataGranularity": "1d", "exchangeTimezoneName": "America/New_York"},
            "timestamp": [ts],
            "indicators": {"quote": [{"close": [123.0], "high": [125.5]}]},
        }]}}
        with patch.object(sources, "provider_available", return_value=True), \
             patch.object(sources, "_yahoo_headers", return_value={}), \
             patch.object(sources, "_yahoo_pace"), \
             patch.object(sources, "_yahoo_session_crumb", return_value=None), \
             patch.object(sources, "_get_json", return_value=payload) as get, \
             patch.object(sources, "note_provider_ok"):
            rows, src = sources.index_drawdown_history("^990100-USD-STRD", None)
        self.assertEqual(rows[0][1], 123.0)
        self.assertEqual(rows[0][2], 125.5)
        called_url = get.call_args.args[0]
        self.assertIn("period1=0", called_url)
        self.assertIn("interval=1d", called_url)
        self.assertNotIn("range=max", called_url)
        self.assertIn("%5E990100-USD-STRD", called_url)
        self.assertIn("Yahoo", src)

        # Yahoo's silent `1mo` coercion must be rejected!
        monthly = {"meta": {"dataGranularity": "1mo"}, "timestamp": [ts],
                   "indicators": {"quote": [{"close": [123.0]}]}}
        with self.assertRaises(ValueError):
            sources._parse_chart_result_ohlc(monthly)

    def test_us_index_cascade_sina_nasdaq_tencent_before_fred(self):
        today_iso = date.today().isoformat()
        today_us = date.today().strftime("%m/%d/%Y")
        sina_body = f'var s=([{{"d":"{today_iso}","o":"5950","h":"6020.5","l":"5940","c":"5990.25","v":"100"}}]);'
        nasdaq_body = json.dumps({"data": {"tradesTable": {"rows": [
            {"date": today_us, "open": "20,500.00", "high": "20,850.50",
             "low": "20,400.00", "close": "20,700.25", "volume": "1000"},
        ]}}})
        tencent_body = json.dumps({"data": {"us.DJI": {"day": [
            [today_iso, "43000.0", "43250.5", "43400.0", "42950.0", "1000"],
        ]}}})

        # 1. Yahoo cooling down -> Sina US daily K serves ^GSPC with OHLC
        sources.note_provider_failure("yahoo", "HTTP 429", rate_limited=True)
        with patch.object(sources, "_stock_host_get", return_value=sina_body) as host_get:
            rows, src = sources.index_drawdown_history("^GSPC", "SP500")
        self.assertEqual(rows[-1], (today_iso, 5990.25, 6020.5))
        self.assertIn("Sina", src)
        self.assertIn("symbol=.INX", host_get.call_args.args[0])

        # 2. Sina fails -> Nasdaq serves ^NDX with OHLC
        def sina_fail_then_nasdaq(url, key, **kwargs):
            if "sina" in key:
                raise RuntimeError("sina down")
            return nasdaq_body

        with patch.object(sources, "_stock_host_get", side_effect=sina_fail_then_nasdaq):
            rows, src = sources.index_drawdown_history("^NDX", "NASDAQ100")
        self.assertEqual(rows[-1], (today_iso, 20700.25, 20850.5))
        self.assertIn("Nasdaq", src)

        # 3. Sina fails -> Tencent raw kline serves ^DJI with OHLC
        def sina_fail_then_tencent(url, key, **kwargs):
            if "sina" in key:
                raise RuntimeError("sina down")
            return tencent_body

        with patch.object(sources, "_stock_host_get", side_effect=sina_fail_then_tencent):
            rows, src = sources.index_drawdown_history("^DJI", "DJIA")
        self.assertEqual(rows[-1], (today_iso, 43250.5, 43400.0))
        self.assertIn("Tencent", src)

    def test_msci_official_api_fallback_and_strict_variant_validation(self):
        today_msci = date.today().strftime("%Y%m%d")
        prev_msci = (date.today() - timedelta(days=1)).strftime("%Y%m%d")
        valid_payload = json.dumps({
            "msci_index_code": "990100",
            "index_variant_type": "STRD",
            "ISO_currency_symbol": "USD",
            "indexes": {"INDEX_LEVELS": [
                {"calc_date": int(prev_msci), "level_eod": 3820.5},
                {"calc_date": int(today_msci), "level_eod": 3790.25},
            ]},
        })
        sources.note_provider_failure("yahoo", "HTTP 429", rate_limited=True)
        with patch.object(sources, "_get", return_value=valid_payload) as get_fn:
            rows, src = sources.index_drawdown_history("^990100-USD-STRD", None)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1][1], 3790.25)
        self.assertEqual(len(rows[-1]), 2)  # close-only EOD
        self.assertIn("MSCI (990100 USD STRD", src)
        called_url = get_fn.call_args.args[0]
        self.assertIn("index_codes=990100", called_url)
        self.assertIn("index_variant=STRD", called_url)
        self.assertIn("baseValue=false", called_url)

        # Reject NETR / total-return mismatch or HTTP-200 error_message:
        for bad in (
            {"error_code": "400", "error_message": "Invalid start_date"},
            {"msci_index_code": "990100", "index_variant_type": "NETR",
             "ISO_currency_symbol": "USD",
             "indexes": {"INDEX_LEVELS": [{"calc_date": int(today_msci), "level_eod": 9000.0}]}},
            {"msci_index_code": "892400", "index_variant_type": "STRD",
             "ISO_currency_symbol": "USD",
             "indexes": {"INDEX_LEVELS": [{"calc_date": int(today_msci), "level_eod": 3790.0}]}},
        ):
            with self.assertRaises(ValueError):
                sources._parse_msci_levels_payload(bad, index_code="990100")

    def test_fred_fallback_and_msci_no_proxy(self):
        rows = [(date.today().isoformat(), 5000)]
        with patch.object(sources, "provider_available", return_value=False), \
             patch.object(sources, "_fred_csv_series", return_value=rows) as fred:
            result, src = sources.index_drawdown_history("^GSPC", "SP500")
            self.assertEqual(result, rows)
            self.assertIn("up to 10 years", src)
            fred.assert_called_once_with("SP500", window_years=10)
            with self.assertRaises(RuntimeError):
                sources.index_drawdown_history("^990100-USD-STRD", None)
            fred.assert_called_once()

    def test_index_quotes_batch_sina_et_trade_date_and_fallbacks(self):
        # Sina GMT+8 timestamp is Saturday morning (2026-11-21 05:10:02 MYT),
        # while the US market close was Friday 2026-11-20 ("Nov 20 05:10PM EST").
        sina_txt = (
            'var hq_str_gb_$inx="S&P 500,5969.34,0.35,2026-11-21 05:10:02,20.63,'
            '5955.12,5978.50,5940.10,6017.31,4800.00,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
            'Nov 20 05:10PM EST,5948.71,0,0,2026";\n'
            'var hq_str_gb_$ndx="Nasdaq 100,20750.00,0.50,2026-11-21 05:10:02,103.23,'
            '20680.00,20810.00,20600.00,21182.00,16000.00,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
            'Nov 20 05:10PM EST,20646.77,0,0,2026";\n'
            'var hq_str_gb_$dji="Dow Jones,44296.51,0.97,2026-11-21 05:10:02,426.16,'
            '43900.00,44323.00,43880.00,44486.70,36000.00,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
            'Nov 20 05:10PM EST,43870.35,0,0,2026";\n'
        )
        sources.note_provider_failure("yahoo", "HTTP 429", rate_limited=True)
        with patch.object(sources, "_stock_host_get", return_value=sina_txt):
            res = sources.index_quotes_batch(
                now=datetime(2026, 11, 21, 1, 0, tzinfo=timezone.utc)
            )
        self.assertEqual(res["status"], "partial")  # 3 US indices live, MSCI daily EOD
        spx = res["items"]["^GSPC"]
        self.assertEqual(spx["price"], 5969.34)
        self.assertEqual(spx["prev_close"], 5948.71)
        self.assertEqual(spx["day_high"], 5978.50)
        self.assertEqual(spx["week52_high"], 6017.31)
        self.assertEqual(spx["trade_date"], "2026-11-20")

        # Fallback to Tencent when Sina + Nasdaq fail:
        tencent_txt = (
            'v_usINX="200~SPX~.INX~5969.34~5948.71~5955.12~0~0~0~0~0~0~0~0~0~0~0~0~0~0~'
            '0~0~0~0~0~0~0~0~0~0~2026-11-20 16:05:00~20.63~0.35~5978.50~5940.10~0";'
        )

        def quote_cascade(url, key, **kwargs):
            if key in (sources.SINA_INDEX_QUOTE_KEY, sources.NASDAQ_INDEX_QUOTE_KEY):
                raise RuntimeError("down")
            return tencent_txt

        with patch.object(sources, "_stock_host_get", side_effect=quote_cascade):
            res2 = sources.index_quotes_batch(["^GSPC"],
                                              now=datetime(2026, 11, 20, 21, 10, tzinfo=timezone.utc))
        self.assertEqual(res2["status"], "ok")
        self.assertEqual(res2["items"]["^GSPC"]["price"], 5969.34)
        self.assertEqual(res2["items"]["^GSPC"]["day_high"], 5978.50)
        self.assertEqual(res2["items"]["^GSPC"]["trade_date"], "2026-11-20")

    def test_cache_failure_preserves_last_good_and_merges_live_quotes(self):
        key = "drawdown:^GSPC"
        now_dt = datetime(2026, 11, 20, 16, 0, tzinfo=timezone.utc)
        hist_item = drawdown.from_history("S&P 500", [
            ("2026-11-18", 6000.0, 6025.0),
            ("2026-11-19", 5900.0, 5950.0),
        ], "Sina", today=date(2026, 11, 20))
        with patch.dict(app.CACHE, {}, clear=True), \
             patch.object(app, "save_disk_cache"), \
             patch.object(config, "DRAWDOWN_QUOTES_ENABLED", True):
            app.cache_put(key, hist_item, ttl=drawdown.TTL)
            original = app.cache_get(key)["at"]

            def fail():
                raise RuntimeError("offline")

            app.fetch_into_cache(key, fail, drawdown.TTL)
            self.assertEqual(app.cache_get(key)["at"], original)
            item = app.drawdown_view(now=now_dt)["items"][0]
            self.assertEqual(item["status"], "cached")
            self.assertEqual(item["peak"], 6000.0)
            self.assertEqual(app.drawdown_view(now=now_dt)["items"][2]["status"], "unavailable")

            # Overlay live quote from drawdown_quotes cache:
            app.cache_put("drawdown_quotes", {
                "status": "partial",
                "message": "Live quotes loaded",
                "fetched_at_utc": "2026-11-20T15:58:00+00:00",
                "coverage": {"symbols": 4, "with_quotes": 1, "providers": ["Sina US index quote"]},
                "items": {
                    "^GSPC": {
                        "symbol": "^GSPC",
                        "price": 5950.0,
                        "prev_close": 5900.0,
                        "change": 50.0,
                        "change_pct": 0.85,
                        "day_high": 6050.0,
                        "day_low": 5910.0,
                        "trade_date": "2026-11-20",
                        "as_of_utc": "2026-11-20T15:58:00+00:00",
                        "market_status": "regular",
                        "source": "Sina US index quote",
                    }
                },
            }, ttl=300)
            merged = app.drawdown_view(now=now_dt)["items"][0]
            self.assertTrue(merged["current_is_live_quote"])
            self.assertEqual(merged["current"], 5950.0)
            self.assertEqual(merged["intraday_peak"], 6050.0)
            self.assertEqual(merged["close_peak"], 6000.0)
            self.assertAlmostEqual(merged["close_drawdown_pct"], (5950.0 / 6000.0 - 1.0) * 100.0)

    def test_render_escapes_source_and_labels_coverage(self):
        html = render.build_drawdown_page({"items": [{
            "name": "<index>",
            "current": 90,
            "current_is_live_quote": True,
            "daily_close": 88,
            "daily_close_date": "2026-11-19",
            "current_date": "2026-11-20",
            "peak": 100,
            "drawdown_pct": -10,
            "close_peak": 100,
            "close_peak_date": "2026-11-18",
            "close_drawdown_pct": -10,
            "intraday_peak": 105,
            "intraday_peak_date": "2026-11-17",
            "intraday_drawdown_pct": -14.2857,
            "source": "<source>",
            "status": "cached",
            "quote": {
                "price": 90,
                "change": 2.0,
                "change_pct": 2.27,
                "source": "<qsrc>",
                "as_of_utc": "2026-11-20T16:00:00+00:00",
            },
        }]})
        self.assertIn("&lt;index&gt;", html)
        self.assertIn("&lt;source&gt;", html)
        self.assertIn("&lt;qsrc&gt;", html)
        self.assertNotIn("<source>", html)
        self.assertNotIn("<qsrc>", html)
        self.assertIn("Intraday high", html)
        self.assertIn("From intraday high", html)
        self.assertIn("Closing high", html)
        self.assertIn("From closing high", html)
        self.assertIn("-14.29%", html)
        self.assertIn("-10.00%", html)
        self.assertIn("History bar close:", html)
        self.assertIn("not a verified all-time", html)
        self.assertIn("CACHED / STALE", html)
        self.assertIn('aria-current="page"', html)
        self.assertIn("Fetch latest values", html)
        self.assertIn("/drawdowns?refresh=1", html)

    def test_manual_refresh_route_fetches_drawdowns_then_cleans_url(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with patch.object(app, "refresh_drawdowns") as refresh:
                # urllib follows the 303, proving the browser lands on the clean
                # URL instead of forcing another provider fetch on auto-refresh.
                with urlopen(base + "/drawdowns?refresh=1") as response:
                    self.assertEqual(response.geturl(), base + "/drawdowns")
                    self.assertIn("Index drawdowns", response.read().decode())
                refresh.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_refresh_drawdowns_resets_index_quote_breakers_and_refreshes_quotes(self):
        sources.note_provider_failure(sources.SINA_INDEX_QUOTE_KEY, "temp error", unreachable=True)
        sources.note_provider_failure(sources.MSCI_KEY, "temp error", unreachable=True)
        self.assertFalse(sources.provider_available(sources.SINA_INDEX_QUOTE_KEY))
        self.assertFalse(sources.provider_available(sources.MSCI_KEY))
        called = []

        def fake_fetch(key, fn, ttl):
            called.append(key)

        with patch.object(config, "DEMO", False), \
             patch.object(app, "fetch_into_cache", side_effect=fake_fetch):
            app.refresh_drawdowns()
        self.assertTrue(sources.provider_available(sources.SINA_INDEX_QUOTE_KEY))
        self.assertTrue(sources.provider_available(sources.MSCI_KEY))
        self.assertIn("drawdown_quotes", called)
        self.assertEqual(sum(1 for k in called if k.startswith("drawdown:")), 4)

    def test_warm_all_immediately_upgrades_legacy_close_only_drawdown_cache(self):
        now_ts = time.time()
        with patch.dict(app.CACHE, {}, clear=True), \
             patch.object(app, "save_disk_cache"), \
             patch.object(config, "DEMO", True):
            for key, _fn, ttl in app.REGISTRY:
                if key == "drawdown:^GSPC":
                    # Pre-upgrade schema lacking `close_peak`: should refresh immediately!
                    app.CACHE[key] = {
                        "data": {"name": "S&P 500", "current": 5000, "peak": 5100, "drawdown_pct": -1.96},
                        "at": now_ts,
                        "ttl": ttl,
                        "err": None,
                    }
                else:
                    app.CACHE[key] = {"data": {"close_peak": 1, "close": 1}, "at": now_ts, "ttl": ttl, "err": None}
            refreshed = []
            with patch.object(app, "fetch_into_cache", side_effect=lambda k, fn, ttl: refreshed.append(k)):
                app.warm_all(force=False)
            self.assertEqual(refreshed, ["drawdown:^GSPC"])

    def test_demo_drawdowns_and_doctor_probe(self):
        rows, src = demo.index_drawdown_history("^GSPC", "SP500")
        self.assertEqual(len(rows[-1]), 3)
        self.assertIn("OHLC", src)
        msci_rows, _ = demo.index_drawdown_history("^990100-USD-STRD", None)
        self.assertEqual(len(msci_rows[-1]), 2)
        q_res = demo.index_quotes_batch()
        self.assertIn("^GSPC", q_res["items"])
        self.assertTrue(q_res["items"]["^GSPC"]["demo"])

        labels = []
        sources._doctor_drawdowns(lambda checks, width=28: labels.extend(lbl for lbl, _ in checks))
        self.assertIn("msci 990100 USD STRD", labels)
        self.assertIn("sina index quotes", labels)

    def test_html_and_json_routes(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/drawdowns") as response:
                self.assertIn("Index drawdowns", response.read().decode())
            with urlopen(base + "/api/drawdowns") as response:
                data = json.load(response)
                self.assertEqual(len(data["items"]), 4)
                self.assertEqual(data["items"][2]["name"], "MSCI World")
                self.assertIn("quotes", data)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
