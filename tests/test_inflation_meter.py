"""Offline tests for the Summary tab's inflation meter (no network).

Regression guard for the meter silently showing the wrong country: the World
Bank CPI list is sorted by value, so its first row is the *highest* inflation
country, not the United States. The meter must fall back to the US row only,
say which series it is showing, and never clamp a real print into a fake 5%.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import config  # noqa: E402
import demo  # noqa: E402
import render  # noqa: E402


def _cpi_rows():
    """Same shape as sources.worldbank_indicator: sorted by value, India first."""
    return [
        {"code": "IND", "name": "India", "year": "2024", "value": 4.9},
        {"code": "USA", "name": "United States", "year": "2024", "value": 2.9},
        {"code": "MYS", "name": "Malaysia", "year": "2024", "value": 2.1},
    ]


def _sections(*, breakeven, cpi):
    return {
        "indices": [], "commodities": [], "fx": [],
        "cpi": cpi, "gdp": [],
        "policy_pairs": config.FRED_POLICY,
        "policy_series": {"DFF": [("2026-11-27", 4.83)]},
        "curve": [], "curve_meta": ("T10Y2Y", "US 10Y − 2Y Treasury Spread", 4),
        "breakeven": breakeven,
        "breakeven_meta": ("T10YIE", "US 10Y Breakeven Inflation"),
        "real_yield": [], "real_yield_meta": ("DFII10", "US 10Y Real Yield"),
    }


def _meter(page):
    """(value, caption, note) of the summary page's inflation meter."""
    start = page.find("Inflation meter")
    block = page[start:start + 4000]
    value = re.search(r'inflation meter ([^"]+) percent"', block).group(1)
    caption = re.search(r'y="96"[^>]*>([^<]+)</text>', block).group(1)
    note = re.search(r'<p class="meta">(.*?)</p>', block, re.S).group(1)
    return float(value), caption, note


class InflationGaugeTests(unittest.TestCase):
    def test_needle_is_clamped_but_the_printed_number_is_not(self):
        svg = render.inflation_gauge(8.0, "US CPI · 2022")
        self.assertIn('aria-label="inflation meter 8.00 percent"', svg)
        self.assertIn(">8.00%</text>", svg)          # the real print, not 5.00%
        self.assertIn("above the 0–5% scale", svg)

    def test_in_scale_value_is_unchanged(self):
        svg = render.inflation_gauge(2.36, "US 10y breakeven")
        self.assertIn(">2.36%</text>", svg)
        self.assertIn("US 10y breakeven · near a 2% anchor", svg)
        self.assertNotIn("scale", svg)

    def test_missing_or_non_numeric_value_says_so(self):
        for value in (None, "n/a", float("nan")):
            self.assertIn("Inflation meter needs", render.inflation_gauge(value))


class SummaryMeterTests(unittest.TestCase):
    def test_breakeven_wins_and_is_labelled_us(self):
        page = render.build_summary_page(_sections(
            breakeven=[("2026-11-27", 2.36)], cpi=_cpi_rows()))
        value, caption, note = _meter(page)
        self.assertEqual(value, 2.36)
        self.assertIn("US 10y breakeven", caption)
        self.assertIn("expected inflation", note)

    def test_fallback_uses_the_us_row_not_the_highest_inflation_country(self):
        page = render.build_summary_page(_sections(breakeven=[], cpi=_cpi_rows()))
        value, caption, note = _meter(page)
        self.assertEqual(value, 2.90)          # US, not India's 4.90
        self.assertNotIn("4.90", page)
        self.assertIn("US CPI · 2024", caption)
        self.assertIn("trailing 12-month", note)

    def test_everything_missing_is_graceful(self):
        page = render.build_summary_page(_sections(breakeven=[], cpi=[]))
        self.assertIn("Inflation meter needs", page)
        self.assertIn("Inflation meter", page)

    def test_us_row_lookup_accepts_iso3_and_name_variants(self):
        self.assertEqual(render.us_cpi_row(_cpi_rows())["code"], "USA")
        self.assertEqual(render.us_cpi_row(
            [{"code": "USA", "name": ""}])["code"], "USA")
        self.assertEqual(render.us_cpi_row(
            [{"code": "", "name": "United States"}])["name"], "United States")
        self.assertIsNone(render.us_cpi_row([{"code": "IND", "name": "India"}]))


class DemoAnnualYearTests(unittest.TestCase):
    def test_demo_years_are_fixed_and_the_us_2022_spike_lands_in_2022(self):
        series = demo.worldbank_history(["US"], config.WB_CPI)["US"]["series"]
        self.assertEqual(series[0][0], 2014)
        self.assertEqual(series[-1][0], demo._DEMO_ANNUAL_LAST_YEAR)
        by_year = dict(series)
        self.assertEqual(by_year[2022], 8.0)   # US CPI spike, not 2021
        self.assertEqual(by_year[2020], 1.2)


if __name__ == "__main__":
    unittest.main()
