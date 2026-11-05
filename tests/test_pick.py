"""Offline tests for the Pick tab — selection, levels, MYT timing, rendering."""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import pick  # noqa: E402
import render  # noqa: E402

NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)   # 14:00 MYT, 01:00 EST


def _candidate(symbol="AAA", **over):
    """A candidate shaped exactly like stocks.build_screen() output."""
    base = {
        "symbol": symbol,
        "name": f"{symbol} Corp",
        "sector": "Industrials",
        "close": 30.0,
        "as_of": "2026-11-04",
        "ret1_pct": 0.4,
        "ret5_pct": 1.2,
        "ret7_pct": 2.0,
        "ret20_pct": 4.0,
        "ret60_pct": 9.0,
        "sma20": 29.2,
        "sma50": 28.0,
        "vol20_annual_pct": 24.0,
        "drawdown60_pct": -6.0,
        "avg_dollar_volume20": 40_000_000.0,
        "risk": "Medium",
        "rank_score": 3.1,
        "reasons": ["20-session momentum +4.0%"],
        "source": "test",
        "quote_age_days": 0,
        "recent_bars": [["2026-10-28", 28.9], ["2026-11-04", 30.0]],
    }
    base.update(over)
    return base


def _screen(*candidates, **over):
    payload = {
        "status": "ok",
        "message": "3 stocks passed",
        "data_date": "2026-11-04",
        "candidates": list(candidates),
        "currency_reference": {
            "available": True, "rate": 4.21, "source": "ECB", "rate_date": "2026-11-04",
        },
        "max_candidates": 7,
    }
    payload.update(over)
    return payload


class SelectionTests(unittest.TestCase):
    def test_scores_are_ordered_and_normalised_inside_the_list(self):
        strong = _candidate("STRONG", ret20_pct=9.0, ret60_pct=20.0,
                            vol20_annual_pct=18.0, drawdown60_pct=-3.0,
                            avg_dollar_volume20=200_000_000.0)
        weak = _candidate("WEAK", ret20_pct=0.5, ret60_pct=1.0,
                          vol20_annual_pct=70.0, drawdown60_pct=-30.0,
                          avg_dollar_volume20=6_000_000.0, risk="High")
        view = pick.build_pick_view(_screen(weak, strong), now=NOW)
        self.assertEqual([row["symbol"] for row in view["picks"]], ["STRONG", "WEAK"])
        for row in view["picks"]:
            self.assertGreaterEqual(row["pick_score"], 0.0)
            self.assertLessEqual(row["pick_score"], 100.0)
            self.assertEqual(set(row["score_parts"]),
                             {"trend", "entry", "calm", "drawdown", "liquidity"})
        self.assertEqual(view["top"]["symbol"], "STRONG")

    def test_buyable_now_prefers_a_name_inside_its_zone(self):
        # Same trend/vol; one has a delayed quote inside the zone, one far above it.
        inside = _candidate("IN", quote={"price": 29.7})
        chasing = _candidate("CHASE", quote={"price": 36.0})
        view = pick.build_pick_view(_screen(inside, chasing), now=NOW)
        rows = {row["symbol"]: row for row in view["picks"]}
        self.assertGreater(rows["IN"]["score_parts"]["entry"],
                           rows["CHASE"]["score_parts"]["entry"])
        self.assertEqual(rows["IN"]["now_tag"], "in zone")
        self.assertEqual(rows["CHASE"]["now_tag"], "wait")
        self.assertIn("above the no-chase line", rows["CHASE"]["verdict"])

    def test_verdict_reflects_the_delayed_quote(self):
        zone = _candidate("Z", quote={"price": 29.75})
        view = pick.build_pick_view(_screen(zone, _candidate("Y")), now=NOW)
        top = next(r for r in view["picks"] if r["symbol"] == "Z")
        self.assertIn("inside the buy zone", top["verdict"].lower())
        self.assertEqual(top["now_tag"], "in zone")

        crashed = _candidate("C", quote={"price": 26.0})
        view = pick.build_pick_view(_screen(crashed, _candidate("D")), now=NOW)
        top = next(r for r in view["picks"] if r["symbol"] == "C")
        self.assertIn("invalidation line", top["verdict"].lower())
        self.assertEqual(top["now_tag"], "void")

    def test_verdict_distinguishes_slightly_high_from_below_the_zone(self):
        levels = {"buy_zone_low": 29.70, "buy_zone_high": 30.00, "stop": 29.25,
                  "invalidate_below": 29.0, "chase_above": 30.42}
        above, tag = pick._verdict({"quote_price": 30.20, "close": 30.20}, levels)
        self.assertEqual(tag, "slightly high")
        self.assertIn("just above the buy zone", above)

        below, tag = pick._verdict({"quote_price": 29.40, "close": 29.40}, levels)
        self.assertEqual(tag, "below zone")
        self.assertIn("under the buy zone", below)

        chase, tag = pick._verdict({"quote_price": 31.00, "close": 31.00}, levels)
        self.assertEqual(tag, "wait")
        self.assertIn("no-chase line", chase)

    def test_candidates_without_volatility_are_skipped_not_invented(self):
        bad = _candidate("BAD", vol20_annual_pct=None, close=None)
        view = pick.build_pick_view(_screen(_candidate("OK"), bad), now=NOW)
        self.assertEqual([row["symbol"] for row in view["picks"]], ["OK"])
        self.assertEqual(view["skipped_count"], 1)

    def test_empty_screen_degrades_to_a_message(self):
        view = pick.build_pick_view(
            {"status": "waiting", "message": "Waiting for the first refresh.",
             "candidates": []}, now=NOW)
        self.assertIsNone(view["top"])
        self.assertEqual(view["picks"], [])
        self.assertEqual(view["status"], "waiting")
        self.assertIn("Waiting for the first refresh", view["message"])
        # The session block is still useful without any candidates.
        self.assertTrue(view["session"]["entry_window"])


class LevelTests(unittest.TestCase):
    def test_levels_are_ordered_and_risk_is_bounded(self):
        view = pick.build_pick_view(_screen(_candidate()), now=NOW)
        levels = view["top"]["levels"]
        t1, t2 = levels["targets"]
        self.assertLess(levels["stop"], levels["buy_zone_low"])
        self.assertLessEqual(levels["buy_zone_low"], levels["buy_reference"])
        self.assertLessEqual(levels["buy_reference"], levels["buy_zone_high"])
        self.assertLess(levels["buy_zone_high"], t1["price"])
        self.assertLess(t1["price"], t2["price"])
        self.assertGreater(levels["risk_reward"], 1.0)
        self.assertLessEqual(abs(levels["stop_pct_from_entry"]), 8.0)

    def test_quiet_stock_gets_the_minimum_stop_floor(self):
        quiet = _candidate("QUIET", vol20_annual_pct=4.0)
        view = pick.build_pick_view(_screen(quiet), now=NOW,
                                    min_stop_pct=2.5, max_stop_pct=8.0)
        levels = view["top"]["levels"]
        self.assertAlmostEqual(abs(levels["stop_pct_from_entry"]), 2.5, places=2)

    def test_jumpy_stock_is_capped_at_the_maximum_loss(self):
        jumpy = _candidate("JUMP", vol20_annual_pct=180.0)
        view = pick.build_pick_view(_screen(jumpy), now=NOW,
                                    max_stop_pct=8.0, min_stop_pct=2.5)
        levels = view["top"]["levels"]
        self.assertAlmostEqual(abs(levels["stop_pct_from_entry"]), 8.0, places=2)

    def test_net_figures_subtract_the_cost_assumption(self):
        view = pick.build_pick_view(_screen(_candidate()), now=NOW, cost_pct=0.5)
        t1 = view["top"]["levels"]["targets"][0]
        self.assertAlmostEqual(t1["gross_move_pct"] - t1["net_move_pct"], 0.5,
                               places=2)

    def test_myr_values_appear_only_with_a_fresh_rate(self):
        with_fx = pick.build_pick_view(_screen(_candidate()), now=NOW,
                                       currency_rate=4.21)
        levels = with_fx["top"]["levels"]
        self.assertAlmostEqual(levels["buy_reference_myr"],
                               round(levels["buy_reference"] * 4.21, 2), places=2)
        self.assertAlmostEqual(levels["targets"][0]["price_myr"],
                               round(levels["targets"][0]["price"] * 4.21, 2),
                               places=2)

        without_fx = pick.build_pick_view(_screen(_candidate()), now=NOW,
                                          currency_rate=None, demo=True)
        levels = without_fx["top"]["levels"]
        self.assertIsNone(levels["buy_reference_myr"])
        self.assertIsNone(levels["targets"][0]["price_myr"])
        self.assertFalse(without_fx["currency_reference"]["available"])

    def test_hold_estimate_is_a_range_and_follows_the_pace(self):
        view = pick.build_pick_view(_screen(_candidate(ret20_pct=8.0)), now=NOW)
        low, high = view["top"]["hold"]["sessions"]
        self.assertLess(low, high)
        slow = pick.build_pick_view(_screen(_candidate("SLOW", ret20_pct=-4.0)), now=NOW)
        self.assertIn("No positive 20-session drift", slow["top"]["hold"]["text"])


class MalaysiaTimeTests(unittest.TestCase):
    def _session(self, moment):
        return pick.session_plan(moment)

    def test_summer_session_maps_to_2130_myt(self):
        session = self._session(datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(session["session_window_myt"], "21:30–04:00 MYT (next day)")
        # The window does not cross Kuala Lumpur midnight, so it is not labelled
        # "next day"; the date it applies to is reported separately.
        self.assertEqual(session["entry_window"], "21:30–23:00 MYT")
        self.assertEqual(session["avoid_window_myt"], "03:30–04:00 MYT")
        self.assertEqual(session["entry_window_et"], "09:30–11:00 ET")

    def test_standard_time_session_maps_to_2230_myt(self):
        session = self._session(datetime(2026, 12, 15, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(session["session_window_myt"], "22:30–05:00 MYT (next day)")
        self.assertEqual(session["entry_window"], "22:30–00:00 MYT (next day)")

    def test_avoid_window_is_the_last_half_hour(self):
        session = self._session(datetime(2026, 12, 15, 2, 0, tzinfo=timezone.utc))
        self.assertEqual(session["avoid_window_myt"], "04:30–05:00 MYT")
        self.assertTrue(session["entry_window"].startswith("22:30"))

    def test_closed_market_points_at_the_next_window(self):
        session = self._session(datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc))
        self.assertEqual(session["state"], "closed")
        self.assertFalse(session["session_open_now"])
        self.assertIn("next preferred entry window", session["timing"])
        self.assertGreater(session["hours_to_entry"], 0)

    def test_open_market_is_detected_on_the_new_york_clock(self):
        # 15:00 UTC on a Wednesday = 10:00 EST → the session is open and the
        # page is inside the first 90 minutes of it.
        session = self._session(datetime(2026, 12, 16, 15, 0, tzinfo=timezone.utc))
        self.assertEqual(session["state"], "open")
        self.assertTrue(session["in_entry_window_now"])
        self.assertIsNotNone(session["minutes_left_in_entry"])
        self.assertIn("inside the preferred window", session["timing"])

    def test_weekend_is_named_and_skipped(self):
        session = self._session(datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc))  # Sat
        self.assertEqual(session["state"], "weekend")
        self.assertIn("weekend", session["timing"])
        self.assertNotIn("Sat", session["entry_date_local_label"])
        self.assertNotIn("Sun", session["entry_date_local_label"])

    def test_rules_are_printed_for_the_reader(self):
        session = self._session(NOW)
        self.assertGreaterEqual(len(session["rules"]), 4)
        self.assertTrue(any("21:30–04:00" in rule for rule in session["rules"]))
        self.assertTrue(any("Eastern Standard Time" in rule for rule in session["rules"]))


class RenderTests(unittest.TestCase):
    def _view(self, **over):
        return pick.build_pick_view(_screen(_candidate("AAA"), _candidate("BBB")),
                                    now=NOW, currency_rate=4.21, **over)

    def test_page_shows_the_four_answers(self):
        page = render.build_pick_page(self._view(), generated_at=NOW)
        self.assertIn("Buy zone", page)
        self.assertIn("Take-profit 1", page)
        self.assertIn("Hard stop", page)
        self.assertIn("When to buy — Malaysian time", page)
        self.assertIn("MYT", page)
        self.assertIn("How long to hold", page)
        self.assertIn("Pick", page)
        self.assertIn('href="/pick"', page)
        self.assertIn("class=\"on\"", page)
        self.assertEqual(page.count("<tr>"), 3)      # header + two picks

    def test_page_escapes_third_party_text(self):
        evil = _candidate("EVIL", name="<script>alert(1)</script>")
        view = pick.build_pick_view(_screen(evil), now=NOW, currency_rate=4.21)
        page = render.build_pick_page(view, generated_at=NOW)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_page_survives_a_missing_plan(self):
        page = render.build_pick_page(
            {"status": "waiting", "message": "Waiting for the first stock refresh.",
             "picks": [], "top": None, "session": {}}, generated_at=NOW)
        self.assertIn("No plan yet", page)
        self.assertIn("Waiting for the first stock refresh", page)

    def test_disabled_plan_is_stated(self):
        page = render.build_pick_page(
            {"status": "disabled", "message": "The Pick tab is disabled (ECON_PICK=0).",
             "picks": [], "top": None, "session": {}}, generated_at=NOW)
        self.assertIn("ECON_PICK=0", page)
        self.assertNotIn('class="pick-session-bar"', page)
        self.assertNotIn("Take-profit 1", page)

    def test_stale_or_cached_screen_is_badged(self):
        view = self._view()
        view["served_from_cache"] = True
        view["refresh_error"] = "all providers failed"
        page = render.build_pick_page(view, generated_at=NOW)
        self.assertIn("Sources are blocked", page)
        self.assertIn("all providers failed", page)

    def test_demo_banner_and_no_rm_conversion(self):
        view = pick.build_pick_view(_screen(_candidate()), now=NOW, demo=True)
        page = render.build_pick_page(view, demo=True, generated_at=NOW)
        self.assertIn("DEMO MODE — synthetic prices", page)
        self.assertIn("RM figures are omitted", page)

    def test_method_and_costs_are_disclosed(self):
        page = render.build_pick_page(self._view(), generated_at=NOW)
        self.assertIn("How these numbers are calculated", page)
        self.assertIn("plan width", page)
        self.assertIn("round-trip friction", page)
        self.assertIn("not investment advice", page)


class _FakeSummaryTests(unittest.TestCase):
    """app.build_pick() must not fetch anything and must honour the flag."""

    def test_build_pick_reads_the_screen_currency_and_flag(self):
        import app

        screen = _screen(_candidate())
        view = app.build_pick(screen, now=NOW)
        self.assertEqual(view["top"]["symbol"], "AAA")
        self.assertIsNotNone(view["top"]["levels"]["buy_reference_myr"])

        original = app.config.PICK_ENABLED
        try:
            app.config.PICK_ENABLED = False
            disabled = app.build_pick(screen, now=NOW)
        finally:
            app.config.PICK_ENABLED = original
        self.assertEqual(disabled["status"], "disabled")
        self.assertIsNone(disabled["top"])


if __name__ == "__main__":
    unittest.main()
