"""Offline tests for the investment briefing (no network)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import briefing  # noqa: E402
import render  # noqa: E402


def _hist(value):
    return [("2026-10-01", value - 0.1), ("2026-10-07", value)]


def _q(name, close, chg_pct):
    chg = close * chg_pct / 100.0
    return {
        "name": name, "close": close, "chg": chg, "chg_pct": chg_pct,
        "spark": [close * 0.99, close],
        "date": "2026-10-07", "source": "test", "stale": False,
    }


class BriefingTests(unittest.TestCase):
    def test_inverted_curve_is_defensive(self):
        sections = {
            "indices": [_q("S&P 500", 7800, 0.4), _q("Nasdaq 100", 31000, -0.2)],
            "commodities": [_q("Brent Crude", 80, 1.2), _q("Gold", 4100, 0.4)],
            "fx": [_q("Euro (EUR)", 0.86, 0.1)],
            "cpi": [{"code": "US", "name": "United States", "year": "2024", "value": 2.9}],
            "gdp": [{"code": "US", "name": "United States", "year": "2024", "value": 2.1}],
            "policy_series": {"DFF": _hist(3.88), "ECBDFR": _hist(2.5)},
            "curve": _hist(-0.15),
            "breakeven": _hist(2.36),
        }
        out = briefing.build_briefing(sections)
        self.assertEqual(out["posture"], "Defensive")
        self.assertEqual(out["tone"], "risk")
        curve = next(c for c in out["cards"] if c["id"] == "curve")
        self.assertEqual(curve["tone"], "risk")
        self.assertIn("Inverted", curve["text"])

    def test_healthy_board_is_constructive(self):
        sections = {
            "indices": [_q("S&P 500", 7800, 0.6), _q("Nikkei 225", 70000, 0.3)],
            "commodities": [_q("Brent Crude", 80, -0.4), _q("Gold", 4100, -0.2)],
            "fx": [_q("Euro (EUR)", 0.86, -0.1), _q("Yen (JPY)", 148, -0.2)],
            "cpi": [{"code": "US", "name": "United States", "year": "2024", "value": 2.2}],
            "gdp": [
                {"code": "US", "name": "United States", "year": "2024", "value": 2.4},
                {"code": "DE", "name": "Germany", "year": "2024", "value": 1.1},
            ],
            "policy_series": {"DFF": _hist(3.0), "ECBDFR": _hist(2.0)},
            "curve": _hist(0.80),
            "breakeven": _hist(2.20),
        }
        out = briefing.build_briefing(sections)
        self.assertEqual(out["posture"], "Constructive")
        self.assertTrue(out["disclaimer"])

    def test_empty_board_does_not_crash(self):
        out = briefing.build_briefing({})
        self.assertEqual(out["posture"], "Awaiting data")
        self.assertTrue(out["cards"])

    def test_page_renders_briefing_strip(self):
        sections = {
            "indices": [_q("S&P 500", 7800, 0.4)],
            "commodities": [],
            "fx": [],
            "cpi": [],
            "gdp": [],
            "policy_pairs": [],
            "policy_series": {},
            "curve": _hist(0.51),
            "curve_meta": ("T10Y2Y", "US 10Y − 2Y Treasury Spread", 4),
            "breakeven": _hist(2.36),
            "breakeven_meta": ("T10YIE", "US 10Y Breakeven Inflation"),
            "errors": [],
            "providers": [],
        }
        sections["briefing"] = briefing.build_briefing(sections)
        page = render.build_page(sections, demo=True)
        self.assertIn("Investment briefing", page)
        self.assertIn("Not a forecast", page)
        self.assertIn("0.51%", page)


if __name__ == "__main__":
    unittest.main()
