"""Offline drawdown calculations, provider fallback, cache and HTTP tests."""
import json
import sys
import threading
import time
import unittest
from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import app
import drawdown
import render
import sources


class DrawdownTest(unittest.TestCase):
    def test_peak_and_latest_are_different_and_rows_sorted(self):
        item = drawdown.from_history("test", [
            ("2026-11-19", 90), ("2020-01-01", 50),
            ("2026-11-18", 100), ("2026-11-17", None)], "feed",
            today=date(2026, 11, 20))
        self.assertEqual(item["current"], 90)
        self.assertEqual(item["peak"], 100)
        self.assertAlmostEqual(item["drawdown_pct"], -10)
        self.assertEqual(item["peak_date"], "2026-11-18")
        self.assertEqual(item["history_start"], "2020-01-01")

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

    def test_yahoo_max_history_url_and_real_index_symbol(self):
        ts = int(datetime.now(timezone.utc).timestamp())
        payload = {"chart": {"result": [{"timestamp": [ts],
                   "indicators": {"quote": [{"close": [123]}]}}]}}
        with patch.object(sources, "provider_available", return_value=True), \
             patch.object(sources, "_yahoo_headers", return_value={}), \
             patch.object(sources, "_yahoo_pace"), \
             patch.object(sources, "_yahoo_session_crumb", return_value=None), \
             patch.object(sources, "_get_json", return_value=payload) as get, \
             patch.object(sources, "note_provider_ok"):
            rows, src = sources.index_drawdown_history("^990100-USD-STRD", None)
        self.assertEqual(rows[0][1], 123)
        self.assertIn("range=max", get.call_args.args[0])
        self.assertIn("%5E990100-USD-STRD", get.call_args.args[0])
        self.assertIn("Yahoo", src)

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

    def test_cache_failure_preserves_last_good_and_labels_it(self):
        key = "drawdown:^GSPC"
        with patch.dict(app.CACHE, {}, clear=True), patch.object(app, "save_disk_cache"):
            app.cache_put(key, {"name": "S&P 500", "current": 90, "peak": 100,
                                "drawdown_pct": -10}, ttl=drawdown.TTL)
            original = app.cache_get(key)["at"]
            def fail():
                raise RuntimeError("offline")
            app.fetch_into_cache(key, fail, drawdown.TTL)
            self.assertEqual(app.cache_get(key)["at"], original)
            item = app.drawdown_view()["items"][0]
            self.assertEqual(item["status"], "cached")
            self.assertEqual(item["peak"], 100)
            self.assertEqual(app.drawdown_view()["items"][2]["status"], "unavailable")

    def test_render_escapes_source_and_labels_coverage(self):
        html = render.build_drawdown_page({"items": [{"name": "<index>",
            "current": 90, "peak": 100, "drawdown_pct": -10,
            "source": "<source>", "status": "cached"}]})
        self.assertIn("&lt;index&gt;", html)
        self.assertIn("&lt;source&gt;", html)
        self.assertNotIn("<source>", html)
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
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
