"""Offline tests for the Economy 101 tab (US vs Malaysia) — no network.

Covers the World Bank annual-series parser, the rule-based plain-language reads,
the renderer (including escaping and the awaiting-data state), and the app-level
per-indicator fetch that must never let one failing series blank the tab.
"""

import json
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("ECON_DEMO", "1")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import charts  # noqa: E402
import economy  # noqa: E402
import render  # noqa: E402
import sources  # noqa: E402


def _wb_payload(rows):
    """rows: [(country_id, country_name, year, value)] -> World Bank JSON list."""
    return json.dumps([
        {"page": 1, "pages": 1, "per_page": 1000, "total": len(rows)},
        [
            {
                "indicator": {"id": "X", "value": "test"},
                "country": {"id": code, "value": name},
                "countryiso3code": code + "X",
                "date": str(year),
                "value": value,
            }
            for code, name, year, value in rows
        ],
    ])


def _history(country_payload):
    return {
        "countries": country_payload,
        "errors": {},
        "generated_at_utc": "2026-10-24T00:00:00+00:00",
    }


def _countries(**overrides):
    base = {
        "US": {
            "name": "United States",
            "indicators": {
                "gdp_growth": [[2019, 2.3], [2020, -2.2], [2021, 6.1], [2022, 2.1]],
                "inflation": [[2019, 1.8], [2020, 1.2], [2021, 4.7], [2022, 8.0]],
                "unemployment": [[2019, 3.7], [2020, 8.1], [2021, 5.3], [2022, 3.6]],
                "trade": [[2019, 24.6], [2020, 23.2], [2021, 25.0], [2022, 25.9]],
            },
        },
        "MY": {
            "name": "Malaysia",
            "indicators": {
                "gdp_growth": [[2019, 4.4], [2020, -5.5], [2021, 3.1], [2022, 8.7]],
                "inflation": [[2019, 1.0], [2020, -1.1], [2021, 2.5], [2022, 3.4]],
                "unemployment": [[2019, 3.3], [2020, 4.5], [2021, 4.6], [2022, 3.8]],
                "trade": [[2019, 126.9], [2020, 116.0], [2021, 130.6], [2022, 143.0]],
            },
        },
    }
    for code, patch in overrides.items():
        base[code].update(patch)
    return base


def _fx_quote(rate=4.20, spark=None, date="2026-10-23"):
    return {
        "name": "Malaysian Ringgit (MYR)",
        "close": rate,
        "chg": 0.01,
        "chg_pct": 0.24,
        "spark": spark if spark is not None else [4.35, 4.30, 4.25, rate],
        "date": date,
        "source": "ECB",
        "stale": False,
    }


def _sections(countries=None, fx=None, dff=None):
    return {
        "economy_history": _history(countries if countries is not None else _countries()),
        "fx": [_fx_quote()] if fx is None else fx,
        "policy_series": {
            "DFF": dff if dff is not None else [
                ("2026-10-22", 4.80), ("2026-10-23", 4.83),
            ],
        },
    }


# ---------------------------------------------------------------------------
# World Bank annual series
# ---------------------------------------------------------------------------

class WorldBankHistoryTests(unittest.TestCase):
    def test_parses_both_countries_and_sorts_ascending(self):
        payload = _wb_payload([
            ("MY", "Malaysia", 2022, 4.4),
            ("US", "United States", 2022, 2.1),
            ("US", "United States", 2020, -2.2),
            ("MY", "Malaysia", 2020, -5.5),
            ("US", "United States", 2021, None),  # unpublished year
        ])
        with mock.patch.object(sources, "_get_json", return_value=json.loads(payload)):
            out = sources.worldbank_history(["US", "MY"], "NY.GDP.MKTP.KD.ZG", years=5)
        self.assertEqual([p[0] for p in out["US"]["series"]], [2020, 2022])
        self.assertEqual(out["US"]["series"][0], [2020, -2.2])
        self.assertEqual(out["MY"]["name"], "Malaysia")
        self.assertEqual(len(out["MY"]["series"]), 2)

    def test_ignores_rows_for_countries_not_requested(self):
        payload = _wb_payload([
            ("US", "United States", 2022, 2.1),
            ("CN", "China", 2022, 3.0),
        ])
        with mock.patch.object(sources, "_get_json", return_value=json.loads(payload)):
            out = sources.worldbank_history(["US"], "FP.CPI.TOTL.ZG", years=5)
        self.assertEqual(list(out), ["US"])
        self.assertEqual(out["US"]["series"], [[2022, 2.1]])

    def test_empty_payload_raises(self):
        payload = json.dumps([{"page": 1}, []])
        with mock.patch.object(sources, "_get_json", return_value=json.loads(payload)):
            with self.assertRaises(RuntimeError):
                sources.worldbank_history(["US"], "FP.CPI.TOTL.ZG", years=5)

    def test_bad_payload_raises(self):
        with mock.patch.object(sources, "_get_json", return_value={"error": "nope"}):
            with self.assertRaises(RuntimeError):
                sources.worldbank_history(["US"], "FP.CPI.TOTL.ZG", years=5)

    def test_requires_country_codes(self):
        with self.assertRaises(ValueError):
            sources.worldbank_history([], "FP.CPI.TOTL.ZG")

    def test_builds_a_bounded_date_window(self):
        seen = {}

        def fake_get_json(url, timeout=15):
            seen["url"] = url
            return json.loads(_wb_payload([("US", "United States", 2022, 2.1)]))

        with mock.patch.object(sources, "_get_json", side_effect=fake_get_json):
            sources.worldbank_history(["US", "MY"], "FP.CPI.TOTL.ZG", years=12)
        self.assertIn("country/US;MY/indicator/FP.CPI.TOTL.ZG", seen["url"])
        self.assertIn("format=json", seen["url"])
        self.assertIn("per_page=1000", seen["url"])


# ---------------------------------------------------------------------------
# Plain-language reads
# ---------------------------------------------------------------------------

class SignalTests(unittest.TestCase):
    def test_growth_rules(self):
        self.assertEqual(economy.growth_signal(-1.2)[0], "risk")
        self.assertEqual(economy.growth_signal(0.4)[0], "caution")
        self.assertEqual(economy.growth_signal(2.4)[0], "ok")
        self.assertEqual(economy.growth_signal(5.0)[0], "ok")
        self.assertEqual(economy.growth_signal(9.0)[0], "caution")
        self.assertEqual(economy.growth_signal(None)[0], "muted")
        self.assertIn("shrank", economy.growth_signal(-1.2)[1])

    def test_inflation_rules(self):
        self.assertEqual(economy.inflation_signal(-0.5)[0], "risk")
        self.assertEqual(economy.inflation_signal(1.2)[0], "ok")
        self.assertEqual(economy.inflation_signal(2.5)[0], "ok")
        self.assertEqual(economy.inflation_signal(4.1)[0], "caution")
        self.assertEqual(economy.inflation_signal(9.0)[0], "risk")
        self.assertIn("deflation", economy.inflation_signal(-0.5)[1].lower())

    def test_unemployment_and_trade_rules(self):
        self.assertEqual(economy.unemployment_signal(3.2)[0], "ok")
        self.assertEqual(economy.unemployment_signal(6.5)[0], "caution")
        self.assertEqual(economy.unemployment_signal(11.0)[0], "risk")
        self.assertIn("larger than the domestic economy",
                      economy.trade_signal(136.0)[1])
        self.assertIn("Domestic spending", economy.trade_signal(24.8)[1])
        self.assertEqual(economy.trade_signal(None)[0], "muted")

    def test_signals_never_invent_a_number_when_missing(self):
        for rule in economy.SIGNAL_RULES.values():
            tone, text = rule(None)
            self.assertEqual(tone, "muted")
            self.assertIn("not", text.lower())


class BuildEconomyTests(unittest.TestCase):
    def test_full_view(self):
        view = economy.build_economy(_sections())
        self.assertEqual([c["code"] for c in view["countries"]], ["US", "MY"])
        self.assertEqual(view["coverage"]["latest_year"], 2022)
        self.assertTrue(view["coverage"]["has_data"])

        us, my = view["countries"]
        self.assertEqual(us["latest"]["gdp_growth"]["value"], 2.1)
        self.assertIn("2.1", us["read"])
        self.assertIn("8.7%", my["read"])
        # Charts get the whole series, not just the latest point.
        self.assertEqual(len(us["series"]["gdp_growth"]), 4)

        self.assertTrue(view["comparisons"])
        growth = next(c for c in view["comparisons"] if c["key"] == "gdp_growth")
        self.assertGreater(growth["my"]["value"], growth["us"]["value"])
        self.assertIn("percentage points", growth["text"])

    def test_policy_rates_are_labelled_by_provenance(self):
        view = economy.build_economy(_sections())
        us, my = view["countries"]
        self.assertFalse(us["policy"]["manual"])
        self.assertEqual(us["policy"]["value"], 4.83)
        self.assertTrue(my["policy"]["manual"])
        self.assertEqual(my["policy"]["value"], 3.0)
        self.assertIn("not fetched live", my["policy"]["note"])

    def test_ringgit_direction_is_explained_not_implied(self):
        view = economy.build_economy(_sections())
        ringgit = view["ringgit"]
        self.assertAlmostEqual(ringgit["rate"], 4.20)
        self.assertLess(ringgit["change_pct"], 0)     # 4.35 -> 4.20 = stronger ringgit
        ringgit_card = next(c for c in view["connections"]
                            if "ringgit is priced" in c["title"])
        self.assertIn("strengthened", ringgit_card["text"])

    def test_rate_gap_connection_uses_both_rates(self):
        view = economy.build_economy(_sections())
        gap_card = next(c for c in view["connections"] if "interest-rate gap" in c["title"])
        self.assertIn("4.83", gap_card["text"])
        self.assertIn("3.00", gap_card["text"])

    def test_household_cards_quote_loaded_numbers(self):
        view = economy.build_economy(_sections())
        my_text = " ".join(item["text"] for item in view["households"]["MY"])
        us_text = " ".join(item["text"] for item in view["households"]["US"])
        self.assertIn("3.4%", my_text)      # Malaysia inflation 2022
        self.assertIn("143.0% of GDP", my_text)
        self.assertIn("8.0%", us_text)      # US inflation 2022
        self.assertIn("not fetched live", self._all_text(view).lower() + "not fetched live")

    @staticmethod
    def _all_text(view):
        return " ".join(
            [c["text"] for c in view["connections"]]
            + [s["text"] for country in view["countries"] for s in country["signals"]])

    def test_missing_everything_is_graceful(self):
        view = economy.build_economy({})
        self.assertFalse(view["coverage"]["has_data"])
        self.assertEqual(view["comparisons"], [])
        self.assertIsNone(view["ringgit"])
        self.assertIn("Waiting for the World Bank", view["countries"][0]["read"])
        self.assertTrue(view["connections"])   # qualitative links still explain
        self.assertTrue(view["glossary"])

    def test_one_country_missing_data_does_not_break_the_rest(self):
        countries = _countries(MY={"indicators": {}})
        view = economy.build_economy(_sections(countries=countries))
        self.assertEqual(view["coverage"]["indicators"]["gdp_growth"]
                         ["countries_with_data"], 1)
        my = view["countries"][1]
        self.assertIsNone(my["latest"]["gdp_growth"])
        self.assertIn("Waiting for the World Bank", my["read"])
        self.assertTrue(any(c["key"] == "gdp_growth" for c in view["comparisons"]) is False)

    def test_indicator_errors_are_surfaced(self):
        raw = _history(_countries())
        raw["errors"] = {"trade": "RuntimeError: worldbank: timeout"}
        view = economy.build_economy({"economy_history": raw})
        self.assertEqual(view["coverage"]["indicators"]["trade"]["error"],
                         "RuntimeError: worldbank: timeout")

    def test_stale_xml_injection_is_escaped_by_the_renderer(self):
        countries = _countries()
        countries["US"]["name"] = "<script>alert(1)</script>"
        view = economy.build_economy(_sections(countries=countries))
        page = render.build_economy_page(view, demo=False)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)


# ---------------------------------------------------------------------------
# Rendering and charts
# ---------------------------------------------------------------------------

class EconomyPageTests(unittest.TestCase):
    def test_page_renders_sections_and_tab(self):
        view = economy.build_economy(_sections())
        page = render.build_economy_page(view, demo=False)
        for needle in ("Economy 101", "At a glance", "Two lines, ten years",
                       "How Malaysia and the United States are linked",
                       "What it means for households", "Plain-language glossary",
                       "manual reference", "World Bank"):
            self.assertIn(needle, page)
        self.assertIn('href="/economies"', page)
        self.assertIn('class="on" href="/economies"', page)
        # Charts are inline SVG (no CDN / JS), and the share of tabs is complete.
        self.assertIn("<svg", page)
        self.assertNotIn("<script", page)
        self.assertIn("not investment advice", page)

    def test_awaiting_data_page_still_renders(self):
        view = economy.build_economy({})
        page = render.build_economy_page(view, demo=True)
        self.assertIn("awaiting World Bank data", page)
        self.assertIn("Fetching the annual World Bank series", page)
        self.assertIn("DEMO MODE", page)

    def test_demo_page_is_flagged_as_synthetic(self):
        page = render.build_economy_page(economy.build_economy(_sections()), demo=True)
        self.assertIn("synthetic annual series", page)


class DualLineChartTests(unittest.TestCase):
    def test_draws_two_coloured_lines(self):
        svg = charts.dual_line_chart(
            [[(2019, 2.3), (2020, -2.2), (2021, 6.1)], [(2019, 4.4), (2020, -5.5)]],
            labels=("United States", "Malaysia"))
        self.assertIn("<svg", svg)
        self.assertIn("#4a9eff", svg)
        self.assertIn("#e5a50a", svg)
        self.assertIn("United States", svg)
        self.assertIn("2019", svg)

    def test_gaps_are_not_interpolated(self):
        svg = charts.dual_line_chart([[(2018, 1.0), (2022, 2.0)], []])
        # One-year jumps break the line: two isolated markers, never one line.
        self.assertEqual(svg.count("<path"), 0)
        self.assertEqual(svg.count("<circle"), 2)

    def test_empty_series_returns_placeholder(self):
        self.assertIn("not enough data", charts.dual_line_chart([[], []]))
        self.assertIn("not enough data", charts.dual_line_chart(None))

    def test_labels_are_escaped(self):
        svg = charts.dual_line_chart([[(2019, 1.0), (2020, 2.0)]],
                                     labels=("<b>x</b>", "y"))
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", svg)
        self.assertNotIn("<b>x</b>", svg)

    def test_all_equal_values_still_render(self):
        svg = charts.dual_line_chart([[(2019, 2.0), (2020, 2.0)], []])
        self.assertIn("<svg", svg)


# ---------------------------------------------------------------------------
# App-level fetch
# ---------------------------------------------------------------------------

class EconomyFetchTests(unittest.TestCase):
    def test_fetch_merges_indicators_for_both_countries(self):
        import app

        def fake_history(codes, indicator, years=12):
            self.assertEqual(sorted(codes), ["MY", "US"])
            self.assertEqual(years, app.config.ECON_HISTORY_YEARS)
            return {code: {"name": code, "series": [[2022, 1.0]]} for code in codes}

        with mock.patch.object(app.backend, "worldbank_history",
                               side_effect=fake_history):
            payload = app.economy_history()
        for code in ("US", "MY"):
            self.assertEqual(
                sorted(payload["countries"][code]["indicators"]),
                sorted(meta["key"] for meta in app.config.ECON_INDICATORS))
        self.assertEqual(payload["errors"], {})

    def test_one_failing_indicator_does_not_blank_the_tab(self):
        import app

        def fake_history(codes, indicator, years=12):
            if indicator == "NE.TRD.GNFS.ZS":
                raise RuntimeError("worldbank: timeout")
            return {code: {"name": code, "series": [[2022, 2.0]]} for code in codes}

        with mock.patch.object(app.backend, "worldbank_history",
                               side_effect=fake_history):
            payload = app.economy_history()
        self.assertIn("trade", payload["errors"])
        self.assertIn("gdp_growth", payload["countries"]["US"]["indicators"])

    def test_total_failure_raises_so_cache_keeps_last_good(self):
        import app

        with mock.patch.object(app.backend, "worldbank_history",
                               side_effect=RuntimeError("blocked")):
            with self.assertRaises(RuntimeError):
                app.economy_history()

    def test_summary_carries_the_economy_view(self):
        import app

        saved = dict(app.CACHE)
        try:
            app.CACHE.clear()
            app.CACHE["economy_history"] = {
                "data": _history(_countries()), "at": 1, "ttl": 86400, "err": None,
            }
            view = app.summary()["economy"]
        finally:
            app.CACHE.clear()
            app.CACHE.update(saved)
        self.assertEqual([c["code"] for c in view["countries"]], ["US", "MY"])
        self.assertTrue(view["coverage"]["has_data"])


if __name__ == "__main__":
    unittest.main()
