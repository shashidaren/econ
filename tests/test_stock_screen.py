"""Offline tests for the stock watchlist/risk screen and its data adapters."""

import json
import os
import sys
import unittest
from datetime import date, timedelta
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

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


class StockScreenTests(unittest.TestCase):
    def test_ranks_at_most_three_and_reports_risk(self):
        universe = [_spec(f"T{i}", sector=f"Sector {i}") for i in range(5)]
        data = {
            spec[0]: {"rows": _history(start=15 + i, growth=0.003 + i * 0.001),
                      "source": "test"}
            for i, spec in enumerate(universe)
        }
        result = stocks.build_screen(universe, data, limit=20)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["eligible_count"], 5)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertTrue(all(c["risk"] in ("Low", "Medium", "High")
                            for c in result["candidates"]))
        self.assertEqual(result["candidates"][0]["symbol"], "T4")

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

    def test_empty_screen_exposes_unavailable_data_instead_of_fake_picks(self):
        result = stocks.build_screen([_spec("MISSING")], {}, fetch_error="feed blocked")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["candidates"], [])
        self.assertIn("feed blocked", result["fetch_error"])

    def test_stocks_page_caps_display_at_three_and_escapes_text(self):
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
            "data_date": date.today().isoformat(), "source": "test",
            "filters": {"price_min": 5, "price_max_exclusive": 50,
                        "min_avg_dollar_volume": 5_000_000},
            "exclusions": {},
        }, demo=True)
        self.assertEqual(page.count('class="stock-card"'), 3)
        self.assertIn("Stocks", page)
        self.assertNotIn("<script>", page)
        self.assertIn("DEMO MODE — synthetic stock history", page)


class StockDataAdapterTests(unittest.TestCase):
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

    def test_yahoo_chart_parser_keeps_volume_for_screener(self):
        result = {
            "timestamp": [1791417600, 1791504000],
            "indicators": {"quote": [{"close": [10.0, 11.0], "volume": [100, None]}]},
        }
        rows = sources._parse_chart_result_with_volume(result)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1:], (10.0, 100.0))
        self.assertIsNone(rows[1][2])

    def test_stock_history_uses_yahoo_batch_as_fallback_for_tencent_misses(self):
        rows = _history(days=70)
        tencent_rows = [(d, c, v) for d, c, v in rows]

        def fake_tencent(code, days=130):
            if code == "usAAA.OQ":
                return tencent_rows
            raise RuntimeError("Tencent symbol unavailable")

        yahoo_rows = [(d, c, v) for d, c, v in rows]
        with mock.patch.object(sources, "_tencent_us_stock_history", side_effect=fake_tencent), \
                mock.patch.object(sources, "_yahoo_spark_batch", return_value={"BBB": yahoo_rows}) as yahoo, \
                mock.patch.object(sources, "_stooq_us_stock_history") as stooq:
            out = sources.stock_history_batch(
                ["AAA", "BBB"], {"AAA": "usAAA.OQ", "BBB": "usBBB.N"}, days=70)
        self.assertEqual(out["AAA"]["source"], "Tencent K-line")
        self.assertEqual(out["BBB"]["source"], "Yahoo Finance")
        yahoo.assert_called_once_with(["BBB"], days=70, include_volume=True)
        stooq.assert_not_called()


if __name__ == "__main__":
    unittest.main()
