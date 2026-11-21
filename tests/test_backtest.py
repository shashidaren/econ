"""Offline tests for the Pick accuracy engine: features, ensemble, walk-forward
backtesting, forward paper-trading ledger, confidence gating, and rendering."""

from datetime import date, datetime, timedelta, timezone
import math
import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import backtest  # noqa: E402
import pick  # noqa: E402
import render  # noqa: E402
import stocks  # noqa: E402

NOW = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)


def _synth_rows(n=300, *, start_price=20.0, drift=0.0012, amp=0.015,
                period=18, volume=1_500_000.0, start_date=date(2025, 1, 2)):
    """Deterministic synthetic weekday series [(date, close, volume)]."""
    rows = []
    day = start_date
    price = start_price
    while len(rows) < n:
        if day.weekday() < 5:
            i = len(rows)
            cyc = amp * math.sin(2.0 * math.pi * i / period)
            price = max(1.0, price * (1.0 + drift + cyc * 0.15))
            vol = volume * (1.0 + 0.4 * math.cos(2.0 * math.pi * i / 11))
            rows.append((day.isoformat(), round(price, 4), round(vol, 0)))
        day += timedelta(days=1)
    return rows


def _histories():
    return {
        "AAA": _synth_rows(320, start_price=18.0, drift=0.0014, period=17),
        "BBB": _synth_rows(320, start_price=24.0, drift=0.0010, period=21),
        "CCC": _synth_rows(320, start_price=14.0, drift=0.0018, period=13),
        "DDD": _synth_rows(320, start_price=32.0, drift=0.0007, period=25),
        "EEE": _synth_rows(320, start_price=11.0, drift=0.0011, period=19),
        "FFF": _synth_rows(320, start_price=40.0, drift=0.0004, period=29),
    }


class FeatureTests(unittest.TestCase):
    def test_no_lookahead_when_series_truncated(self):
        rows = _synth_rows(140)
        full = backtest.compute_features(rows)
        cutoff = 100
        truncated = backtest.compute_features(rows[:cutoff + 1])
        self.assertEqual(len(full), 140)
        self.assertEqual(len(truncated), cutoff + 1)
        # First 60 bars have None; bar at `cutoff` must match identically.
        self.assertIsNone(full[59])
        self.assertIsNotNone(full[60])
        for key in ("close", "sma20", "sma50", "rsi14", "macd", "macd_signal",
                    "macd_hist", "bollinger_pct_b", "zscore20", "donchian20",
                    "sharpe20", "sortino20", "volume_ratio20", "obv_slope20"):
            self.assertEqual(full[cutoff][key], truncated[cutoff][key],
                             f"look-ahead mismatch on {key}")

    def test_stocks_extra_metrics_match_latest_features(self):
        rows = _synth_rows(130, start_price=22.0, drift=0.0015)
        extra = stocks._metrics(rows)
        feats = backtest.compute_features(rows)[-1]
        for key in ("rsi14", "sharpe20", "sortino20", "macd", "macd_hist",
                    "bollinger_pct_b", "zscore20", "donchian20",
                    "volume_ratio20", "obv_slope20", "dist_high60_pct"):
            if feats[key] is None:
                self.assertIsNone(extra[key])
            else:
                self.assertAlmostEqual(extra[key], feats[key], places=1,
                                       msg=f"mismatch for {key}")


class SignalAndLevelTests(unittest.TestCase):
    def test_signal_families_bounded_and_mean_reversion_gated(self):
        rows = _synth_rows(120, start_price=25.0, drift=0.0012)
        f = backtest.compute_features(rows)[-1]
        score, parts = backtest.ensemble_score(f)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)
        self.assertEqual(set(parts.keys()), set(backtest.SIGNAL_FAMILIES))
        for val in parts.values():
            self.assertGreaterEqual(val, 0.0)
            self.assertLessEqual(val, 1.0)
        # Mean reversion must return 0 when close <= sma50 (no knife-catching).
        broken = dict(f, close=f["sma50"] * 0.95)
        self.assertEqual(backtest.signal_mean_reversion(broken), 0.0)

    def test_regime_gate_suppresses_bear_breadth(self):
        allowed, label = backtest.regime_gate(0.20, bear_threshold=0.35)
        self.assertFalse(allowed)
        self.assertIn("bear", label)
        allowed_ok, _ = backtest.regime_gate(0.65, bear_threshold=0.35)
        self.assertTrue(allowed_ok)

    def test_levels_for_mirrors_pick_levels(self):
        item = {"close": 28.4, "sma20": 27.6, "vol20_annual_pct": 31.5}
        live = pick._levels(item, entry_sigma=0.6, target_sigmas=(1.4, 2.3),
                            max_stop_pct=8.0, min_stop_pct=2.5, cost_pct=0.35)
        bt = backtest.levels_for(item["close"], item["sma20"],
                                 item["vol20_annual_pct"],
                                 entry_sigma=0.6, min_stop_pct=2.5,
                                 max_stop_pct=8.0)
        self.assertAlmostEqual(bt["buy_zone_low"], live["buy_zone_low"], delta=0.015)
        self.assertAlmostEqual(bt["buy_zone_high"], live["buy_zone_high"], delta=0.015)
        self.assertAlmostEqual(bt["buy_reference"], live["buy_reference"], delta=0.015)
        self.assertAlmostEqual(bt["stop"], live["stop"], delta=0.015)
        self.assertAlmostEqual(bt["tp1"], live["targets"][0]["price"], delta=0.015)
        self.assertAlmostEqual(bt["tp2"], live["targets"][1]["price"], delta=0.015)


class StatsAndBacktestTests(unittest.TestCase):
    def test_deflated_sharpe_penalises_multiple_trials(self):
        dsr_few = backtest.deflated_sharpe(1.4, n_trials=2, n_obs=60)
        dsr_many = backtest.deflated_sharpe(1.4, n_trials=50, n_obs=60)
        self.assertIsNotNone(dsr_few)
        self.assertIsNotNone(dsr_many)
        self.assertGreater(dsr_few, dsr_many)
        self.assertIsNone(backtest.deflated_sharpe(1.4, n_trials=1, n_obs=60))
        self.assertIsNone(backtest.deflated_sharpe(1.4, n_trials=8, n_obs=5))

    def test_wilson_and_bootstrap_ci(self):
        lo, hi = backtest.wilson_ci(18, 30)
        self.assertLess(lo, 0.6)
        self.assertGreater(hi, 0.6)
        b_lo, b_hi = backtest.bootstrap_ci([1.0, 2.0, -0.5, 3.0, 1.5, 0.5, 2.5],
                                           n_boot=200, seed=42)
        self.assertLess(b_lo, b_hi)

    def test_walk_forward_backtest_runs_out_of_sample_no_lookahead(self):
        res = backtest.walk_forward_backtest(
            _histories(), horizon=15, step=5, n_folds=3, cost_pct=0.35)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(len(res["folds"]), 3)
        self.assertEqual(res["folds"][0]["chosen"], "equal (no training data yet)")
        self.assertGreater(res["oos"]["n"], 0)
        self.assertIsNotNone(res["oos"]["hit_ci"][0])
        self.assertIsNotNone(res["benchmark"]["n"])
        for t in res["trades"]:
            # Entry must strictly follow decision date (t+1 close).
            self.assertGreater(t["entry_date"], t["date"])
            self.assertAlmostEqual(t["net_pct"], t["gross_pct"] - 0.35, places=6)

    def test_walk_forward_insufficient_history_and_bear_gate(self):
        short = {"AAA": _synth_rows(40)}
        res = backtest.walk_forward_backtest(short)
        self.assertEqual(res["status"], "insufficient_history")
        # Always-bear threshold suppresses all trades.
        bear = backtest.walk_forward_backtest(
            _histories(), horizon=15, step=5, n_folds=3, bear_threshold=1.01)
        self.assertEqual(bear["status"], "no_trades")
        self.assertEqual(bear["oos"]["n"], 0)


class LedgerAndConfidenceTests(unittest.TestCase):
    def test_ledger_save_dedupe_and_t1_maturation(self):
        rows = _synth_rows(120, start_price=20.0, drift=0.001)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.json")
            entry = {
                "date": rows[50][0], "entry_date": None, "symbol": "AAA",
                "entry_ref": rows[51][1], "stop": rows[51][1] * 0.94,
                "tp1": rows[51][1] * 1.07, "tp2": rows[51][1] * 1.12,
                "horizon": 10, "matured": False,
            }
            self.assertTrue(backtest.ledger_append(path, entry))
            # Duplicate (date, symbol) is ignored.
            self.assertFalse(backtest.ledger_append(path, entry))
            mode = stat.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600)

            loaded = backtest.ledger_load(path)
            self.assertEqual(len(loaded), 1)
            # Too early to mature when history stops before t+1+horizon.
            early, n_early = backtest.ledger_mature(
                loaded, {"AAA": rows[:60]}, horizon=10, cost_pct=0.35)
            self.assertEqual(n_early, 0)
            self.assertFalse(early[0]["matured"])

            # Once enough bars exist, matures at t+1 (rows[51][0]).
            matured, n_new = backtest.ledger_mature(
                loaded, {"AAA": rows}, horizon=10, cost_pct=0.35)
            self.assertEqual(n_new, 1)
            self.assertTrue(matured[0]["matured"])
            self.assertEqual(matured[0]["entry_date"], rows[51][0])
            self.assertAlmostEqual(matured[0]["entry"], rows[51][1], places=4)
            # Re-running does not double-mature.
            again, n_again = backtest.ledger_mature(
                matured, {"AAA": rows}, horizon=10, cost_pct=0.35)
            self.assertEqual(n_again, 0)

            summary = backtest.ledger_summary(again)
            self.assertEqual(summary["n_matured"], 1)
            self.assertEqual(summary["n_pending"], 0)

    def test_confidence_gates_until_validated(self):
        # Insufficient samples -> capped at <=39, label "testing".
        c_empty = backtest.confidence_from_evidence(
            {"oos": {"n": 0}}, {"n_matured": 0}, min_n=30)
        self.assertEqual(c_empty["label"], "testing")
        self.assertFalse(c_empty["buy_allowed"])
        self.assertLessEqual(c_empty["score"], 39.0)
        self.assertEqual(c_empty["required_ledger_n"], 30)

        # Strong backtest + enough matured positive live picks -> validated.
        c_ok = backtest.confidence_from_evidence(
            {"oos": {"n": 80, "expectancy_pct": 1.8}, "deflated_sharpe": 0.9,
             "n_trials": 8},
            {"n_matured": 35, "expectancy_pct": 1.4},
            min_n=30, buy_threshold=60.0)
        self.assertEqual(c_ok["label"], "validated")
        self.assertTrue(c_ok["buy_allowed"])
        self.assertGreaterEqual(c_ok["score"], 60.0)

        # Negative backtest expectancy never gets validated.
        c_bad = backtest.confidence_from_evidence(
            {"oos": {"n": 80, "expectancy_pct": -0.6}, "deflated_sharpe": 0.1,
             "n_trials": 8},
            {"n_matured": 35, "expectancy_pct": 1.4},
            min_n=30, buy_threshold=60.0)
        self.assertFalse(c_bad["buy_allowed"])
        self.assertLessEqual(c_bad["score"], 39.0)


class PickAndRenderIntegrationTests(unittest.TestCase):
    def test_pick_gating_and_accuracy_panel_rendering(self):
        cand = {
            "symbol": "AAA", "name": "AAA Corp", "sector": "Tech",
            "close": 25.0, "sma20": 24.4, "sma50": 23.0,
            "ret5_pct": 1.0, "ret20_pct": 4.0, "ret60_pct": 9.0,
            "vol20_annual_pct": 26.0, "drawdown60_pct": -5.0,
            "avg_dollar_volume20": 40_000_000.0,
            "rsi14": 48.0, "macd_hist": 0.25, "sharpe20": 1.3,
            "sortino20": 1.9, "bollinger_pct_b": 0.4, "zscore20": -0.2,
            "donchian20": 0.7, "volume_ratio20": 1.4, "obv_slope20": 0.3,
            "risk": "Medium", "as_of": "2026-11-04",
            "recent_bars": [["2026-10-28", 24.1], ["2026-11-04", 25.0]],
        }
        screen = {"status": "ok", "data_date": "2026-11-04", "candidates": [cand]}
        bt = backtest.walk_forward_backtest(
            _histories(), horizon=15, step=5, n_folds=3)
        led = {"n_matured": 3, "n_pending": 2, "hit_rate": 0.67,
               "hit_ci": (0.2, 0.94), "expectancy_pct": 1.1}
        view = pick.build_pick_view(
            screen, now=NOW, currency_rate=4.21,
            backtest=bt, ledger_summary=led, breadth=0.62)
        self.assertTrue(view["top"]["gated"])
        self.assertIn("Testing", view["top"]["verdict"])
        self.assertIsNotNone(view["top"]["ensemble_score"])
        self.assertIsNotNone(view["top"]["position_hint_pct"])

        html = render.build_pick_page(view, generated_at=NOW)
        self.assertIn("Accuracy — backtest &amp; live testing", html)
        self.assertIn("Confidence", html)
        self.assertIn("Ensemble · Trend", html)
        self.assertIn("Live ledger", html)
        self.assertIn("62% above 20-day average", html)

    def test_app_record_pick_ledger_and_build_pick(self):
        import app
        import config

        cand = {
            "symbol": "AAA", "name": "AAA Corp", "sector": "Tech",
            "close": 25.0, "sma20": 24.4, "sma50": 23.0,
            "ret5_pct": 1.0, "ret20_pct": 4.0, "ret60_pct": 9.0,
            "vol20_annual_pct": 26.0, "drawdown60_pct": -5.0,
            "avg_dollar_volume20": 40_000_000.0, "risk": "Medium",
            "as_of": "2026-11-04",
            "recent_bars": [["2026-10-28", 24.1], ["2026-11-04", 25.0]],
        }
        screen = {
            "status": "ok", "data_date": "2026-11-04",
            "candidates": [cand],
            "currency_reference": {"available": True, "rate": 4.21},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ledger.json")
            orig_file = config.PICK_LEDGER_FILE
            try:
                config.PICK_LEDGER_FILE = path
                self.assertTrue(app._record_pick_ledger(screen))
                self.assertFalse(app._record_pick_ledger(screen))  # deduped
                view = app.build_pick(screen, now=NOW)
                self.assertIsNotNone(view["ledger_summary"])
                self.assertEqual(view["ledger_summary"]["n_pending"], 1)
                self.assertIsNotNone(view["confidence"])
                self.assertFalse(view["confidence"]["buy_allowed"])
            finally:
                config.PICK_LEDGER_FILE = orig_file


if __name__ == "__main__":
    unittest.main()
