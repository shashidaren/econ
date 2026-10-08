"""Offline tests for the data-source cascade, circuit breakers and rendering.

Stdlib only (unittest), no network: every upstream is faked at the single
transport seam (`sources._get`) so the *real* cascade code runs.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import types
import unittest
from datetime import date, timedelta
from unittest import mock

os.environ.setdefault("ECON_DEMO", "1")  # keep app.py in demo mode if imported

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import sources  # noqa: E402


def _csv(rows):
    """Build a FRED-style 'observation_date,VALUE' CSV body."""
    return "observation_date,VALUE\n" + "".join(f"{d},{v}\n" for d, v in rows)


def _spark_payload(pairs):
    """pairs: {yahoo_symbol: [(date, close), ...]} -> v7 spark JSON text."""
    result = []
    for ysym, rows in pairs.items():
        ts, closes = [], []
        for i, (_d, c) in enumerate(rows):
            ts.append(1_700_000_000 + i * 86400)
            closes.append(c)
        result.append({
            "symbol": ysym,
            "response": [{
                "timestamp": ts,
                "indicators": {"quote": [{"close": closes}]},
            }],
        })
    return json.dumps({"spark": {"result": result}})


def _chart_payload(rows):
    ts = [1_700_000_000 + i * 86400 for i in range(len(rows))]
    return json.dumps({"chart": {"result": [{
        "timestamp": ts,
        "indicators": {"quote": [{"close": [c for _, c in rows]}]},
    }]}})


def _recent(n=5, end=None):
    end = end or date.today()
    return [((end - timedelta(days=i)).isoformat(), 100.0 + i) for i in range(n)][::-1]


class TransportCase(unittest.TestCase):
    def setUp(self):
        sources.reset_breakers()
        sources.reset_fred_dead_cache()
        sources.clear_spark_cache()
        sources._YAHOO_LAST_AT = 0.0
        sources._YAHOO_COOKIE = None
        sources._YAHOO_COOKIE_AT = 0.0
        # The cookie handshake would otherwise make a real (slow, blocked) call.
        self._cookie_setting = sources.YAHOO_USE_COOKIE
        sources.YAHOO_USE_COOKIE = False

    def tearDown(self):
        sources.YAHOO_USE_COOKIE = self._cookie_setting
        sources.reset_breakers()
        sources.reset_fred_dead_cache()
        sources.clear_spark_cache()


class TestCurlStatusMarker(TransportCase):
    """_curl_get must surface the HTTP status so 429s are distinguishable."""

    def test_http_status_is_appended_to_error(self):
        fake = types.SimpleNamespace(
            returncode=22,
            stdout=b"\n__HTTP__:429",
            stderr=b"curl: (22) The requested URL returned error: 429",
        )
        with mock.patch.object(sources.subprocess, "run", return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:
                sources._curl_get("https://example.invalid/x")
        self.assertIn("429", str(ctx.exception))
        self.assertIn("HTTP 429", str(ctx.exception))

    def test_status_marker_is_stripped_from_body(self):
        fake = types.SimpleNamespace(
            returncode=0, stdout=b"hello\n__HTTP__:200", stderr=b"")
        with mock.patch.object(sources.subprocess, "run", return_value=fake):
            self.assertEqual(sources._curl_get("https://example.invalid/x"), "hello")

    def test_tcp_connect_failure_does_not_retry_with_urllib(self):
        curl_error = RuntimeError(
            "curl: (28) Failed to connect to stooq.com port 443: Timeout was reached")
        with mock.patch.object(sources, "CURL_BIN", "/usr/bin/curl"), \
                mock.patch.object(sources, "_curl_get", side_effect=curl_error), \
                mock.patch.object(sources, "_urllib_get") as urllib_get:
            with self.assertRaisesRegex(RuntimeError, "Failed to connect"):
                sources._get("https://stooq.com/x", timeout=5)
        urllib_get.assert_not_called()

    def test_http_status_failure_does_not_retry_with_urllib(self):
        curl_error = RuntimeError("curl: (22) error: 404 HTTP 404")
        with mock.patch.object(sources, "CURL_BIN", "/usr/bin/curl"), \
                mock.patch.object(sources, "_curl_get", side_effect=curl_error), \
                mock.patch.object(sources, "_urllib_get") as urllib_get:
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                sources._get("https://fred.example/x", timeout=5)
        urllib_get.assert_not_called()

    def test_tls_fallback_uses_only_the_remaining_total_budget(self):
        curl_error = RuntimeError("curl: (35) SSL_ERROR_SYSCALL")
        with mock.patch.object(sources, "CURL_BIN", "/usr/bin/curl"), \
                mock.patch.object(sources.time, "monotonic", side_effect=[100.0, 101.5]), \
                mock.patch.object(sources, "_curl_get", side_effect=curl_error), \
                mock.patch.object(sources, "_urllib_get", return_value="fallback") as urllib_get:
            self.assertEqual(sources._get("https://example.invalid/x", timeout=4), "fallback")
        self.assertAlmostEqual(urllib_get.call_args.kwargs["timeout"], 3.5)

    def test_classify(self):
        unreachable, limited = sources._classify(
            "curl: (22) The requested URL returned error: 429 HTTP 429")
        self.assertTrue(limited)
        unreachable, limited = sources._classify(
            "curl: (28) Failed to connect to stooq.pl port 443 after 5000 ms")
        self.assertTrue(unreachable)
        self.assertFalse(limited)


class TestBreakers(TransportCase):
    def test_unreachable_failure_starts_immediate_cooldown(self):
        sources.note_provider_failure("stooq:stooq.pl", "Failed to connect", unreachable=True)
        self.assertFalse(sources.provider_available("stooq:stooq.pl"))
        status = {s["provider"]: s for s in sources.provider_status()}
        self.assertEqual(status["stooq:stooq.pl"]["state"], "cooling")
        self.assertGreater(status["stooq:stooq.pl"]["retry_in"], 0)

    def test_success_clears_the_breaker(self):
        sources.note_provider_failure("yahoo", "429", rate_limited=True)
        self.assertFalse(sources.provider_available("yahoo"))
        sources.note_provider_ok("yahoo")
        self.assertTrue(sources.provider_available("yahoo"))
        self.assertEqual(sources.provider_status(), [])

    def test_threshold_needed_for_soft_failures(self):
        sources.note_provider_failure("fred", "no data")
        self.assertTrue(sources.provider_available("fred"))
        sources.note_provider_failure("fred", "no data")
        self.assertFalse(sources.provider_available("fred"))

    def test_reset_one_provider_keeps_other_breakers(self):
        sources.note_provider_failure("fred", "HTTP 429", rate_limited=True)
        sources.note_provider_failure("yahoo:query2.finance.yahoo.com", "Failed to connect",
                                      unreachable=True)
        sources.reset_breakers("yahoo")
        self.assertTrue(sources.provider_available("yahoo:query2.finance.yahoo.com"))
        self.assertFalse(sources.provider_available("fred"))


class TestYahooDoctor(TransportCase):
    def test_missing_cookie_is_reported_as_a_failure_without_exposing_cookie(self):
        with mock.patch.object(sources, "_yahoo_session_cookie", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "no cookie set"):
                sources._doctor_yahoo_cookie()

    def test_chart_checks_run_before_spark_and_each_gets_a_fresh_breaker(self):
        events = []
        real_reset = sources.reset_breakers

        def reset(provider=None):
            events.append(("reset", provider))
            real_reset(provider)

        def chart(symbol, days=30):
            self.assertTrue(sources.provider_available("yahoo"))
            events.append(("chart", symbol))
            sources.note_provider_failure("yahoo", "HTTP 429", rate_limited=True)
            raise RuntimeError("HTTP 429")

        def spark(symbols, days=30):
            self.assertTrue(sources.provider_available("yahoo"))
            events.append(("spark", len(symbols)))
            sources.note_provider_failure("yahoo", "HTTP 429", rate_limited=True)
            raise RuntimeError("HTTP 429")

        def runner(checks, width=18):
            for _label, fn in checks:
                try:
                    fn()
                except Exception:
                    pass  # mirrors the doctor: one failure must not stop later probes

        with mock.patch.object(sources, "reset_breakers", side_effect=reset), \
                mock.patch.object(sources, "_yahoo_session_cookie", return_value="A3=test"), \
                mock.patch.object(sources, "_yahoo_history", side_effect=chart), \
                mock.patch.object(sources, "_yahoo_spark_batch", side_effect=spark):
            sources._doctor_yahoo(runner)

        chart_positions = [i for i, event in enumerate(events) if event[0] == "chart"]
        spark_position = next(i for i, event in enumerate(events) if event[0] == "spark")
        self.assertEqual(len(chart_positions), 3)
        self.assertTrue(all(i < spark_position for i in chart_positions))
        for position in chart_positions + [spark_position]:
            self.assertEqual(events[position - 1], ("reset", "yahoo"))


class TestYahoo(TransportCase):
    def test_cookie_handshake_parses_set_cookie(self):
        sources.YAHOO_USE_COOKIE = True
        fake = types.SimpleNamespace(
            returncode=0,
            stdout=b"HTTP/2 404\r\nset-cookie: A3=d=AQAB; Path=/; Secure\r\n\r\n",
            stderr=b"")
        with mock.patch.object(sources.subprocess, "run", return_value=fake):
            self.assertEqual(sources._yahoo_session_cookie(force=True), "A3=d=AQAB")

    def test_prefetch_skips_unverified_yahoo_aliases(self):
        calls = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            calls.append(url)
            return _spark_payload({"^GSPC": _recent(3)})

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            got = sources.prefetch_yahoo(["^spx", "al.f", "ni.f"], days=30)
        self.assertEqual(got, 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("al.f", calls[0])
        self.assertNotIn("ni.f", calls[0])

    def test_batch_prefetch_feeds_history_without_extra_requests(self):
        calls = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            calls.append(url)
            self.assertIn("/v7/finance/spark", url)
            return _spark_payload({
                "^GSPC": _recent(3),
                "^GDAXI": [(d, v * 2) for d, v in _recent(3)],
            })

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            got = sources.prefetch_yahoo(["^spx", "^dax"], days=30)
            self.assertEqual(got, 2)
            self.assertEqual(len(calls), 1)  # ONE request for both symbols
            hist = sources._yahoo_history("^spx", days=30)
            self.assertEqual(len(calls), 1)  # served from the prefetch cache
            self.assertEqual(len(hist), 3)

    def test_rate_limit_cools_down_and_skips_the_second_host(self):
        calls = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            calls.append(url)
            raise RuntimeError("curl: (22) The requested URL returned error: 429 HTTP 429")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            with self.assertRaises(RuntimeError):
                sources._yahoo_history("^dax", days=30)
            self.assertEqual(len(calls), 1, "must not hammer host 2 after a 429")
            self.assertFalse(sources.provider_available("yahoo"))

            calls.clear()
            with self.assertRaises(RuntimeError) as ctx:
                sources._yahoo_history("^hsi", days=30)
            self.assertIn("cooling down", str(ctx.exception))
            self.assertEqual(calls, [], "cooled provider must cost zero requests")

    def test_cooled_hosts_produce_a_clear_message_and_zero_requests(self):
        for host in ("yahoo:query2.finance.yahoo.com", "yahoo:query1.finance.yahoo.com"):
            sources.note_provider_failure(host, "Failed to connect", unreachable=True)
        calls = []
        with mock.patch.object(sources, "_get",
                               side_effect=lambda *a, **k: calls.append(a)):
            with self.assertRaises(RuntimeError) as ctx:
                sources._yahoo_history("^dax", days=30)
        self.assertIn("cooling down", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_pacing_between_requests(self):
        original = sources.YAHOO_MIN_INTERVAL
        sources.YAHOO_MIN_INTERVAL = 0.25
        self.addCleanup(setattr, sources, "YAHOO_MIN_INTERVAL", original)

        def fake_get(url, timeout=None, headers=None, **kwargs):
            return _chart_payload(_recent(2))

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            t0 = sources.time.time()
            sources._yahoo_history("^spx", days=30)
            sources._yahoo_history("^dax", days=30)
            elapsed = sources.time.time() - t0
        self.assertGreaterEqual(elapsed, 0.25)


class TestFredQuotes(TransportCase):
    def test_stale_series_is_rejected_with_a_useful_message(self):
        old = date.today() - timedelta(days=1800)
        rows = [((old - timedelta(days=i)).isoformat(), 20.0 - i) for i in range(3)][::-1]

        with mock.patch.object(sources, "_get", return_value=_csv(rows)):
            with self.assertRaises(RuntimeError) as ctx:
                sources._fred_quote_history("cl.f", days=30)
        msg = str(ctx.exception)
        self.assertIn("POILWTIUSDM", msg)
        self.assertIn("old", msg)

    def test_fresh_series_is_accepted(self):
        with mock.patch.object(sources, "_get", return_value=_csv(_recent(4))):
            hist = sources._fred_quote_history("^spx", days=30)
        self.assertEqual(len(hist), 4)

    def test_404_widens_once_to_five_years_with_a_shorter_timeout(self):
        seen = []
        timeouts = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            seen.append(url)
            timeouts.append(timeout)
            if len(seen) == 1:
                raise RuntimeError("curl: (22) The requested URL returned error: 404 HTTP 404")
            return _csv(_recent(3))

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            rows = sources._fred_csv_series("SOMEID", window_years=2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(timeouts, [sources.FRED_TIMEOUT, sources.FRED_WIDEN_TIMEOUT])
        retry_start = seen[1].split("cosd=", 1)[1]
        retry_years = (date.today() - date.fromisoformat(retry_start)).days / 365
        self.assertGreater(retry_years, 4.9)
        self.assertLess(retry_years, 5.2)

    def test_confirmed_404_is_negatively_cached_for_the_ttl(self):
        calls = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            calls.append(url)
            raise RuntimeError("curl: (22) The requested URL returned error: 404 HTTP 404")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            with self.assertRaisesRegex(RuntimeError, "negative-cached"):
                sources._fred_csv_series("DEADID", window_years=2)
            self.assertEqual(len(calls), 2, "one initial query plus one bounded retry")
            with self.assertRaisesRegex(RuntimeError, "negative-cached"):
                sources._fred_csv_series("DEADID", window_years=2)
            self.assertEqual(len(calls), 2, "a cached dead ID must cost no new request")
            self.assertIn("DEADID", sources._FRED_DEAD)

    def test_negative_cache_expires(self):
        calls = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            calls.append(url)
            if len(calls) <= 2:
                raise RuntimeError("curl: (22) The requested URL returned error: 404 HTTP 404")
            return _csv(_recent(3))

        with mock.patch.object(sources, "FRED_DEAD_TTL", 10), \
                mock.patch.object(sources.time, "monotonic", side_effect=[100, 100, 105, 111]), \
                mock.patch.object(sources, "_get", side_effect=fake_get):
            with self.assertRaisesRegex(RuntimeError, "negative-cached"):
                sources._fred_csv_series("TEMPID", window_years=2)
            with self.assertRaisesRegex(RuntimeError, "negative-cached"):
                sources._fred_csv_series("TEMPID", window_years=2)
            rows = sources._fred_csv_series("TEMPID", window_years=2)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(rows), 3)

    def test_confirmed_series_mappings_drop_dead_ids_and_allow_monthly_lag(self):
        import config

        self.assertEqual(sources.FRED_QUOTE_MAP["cl.f"], ("POILWTIUSDM", 120))
        self.assertEqual(sources.FRED_QUOTE_MAP["cb.f"], ("DCOILBRENTEU", 10))
        self.assertEqual(sources.FRED_QUOTE_FALLBACKS["cb.f"], (("POILBREUSDM", 120),))
        self.assertEqual(sources.FRED_QUOTE_MAP["hg.f"][1], 120)
        self.assertEqual(sources.FRED_QUOTE_MAP["zw.f"][1], 120)
        configured_ids = {series for _label, series, _age in config.FRED_QUOTES}
        self.assertNotIn("DCOILWTI", configured_ids)
        self.assertNotIn("PSILVUSDM", configured_ids)
        self.assertNotIn("GOLDAMGBD228NLBM", configured_ids)
        self.assertEqual(sources.FRED_PROBE_CANDIDATES, [])

    def test_monthly_wti_series_accepts_the_measured_99_day_age(self):
        last = date.today() - timedelta(days=99)
        rows = [((last - timedelta(days=31)).isoformat(), 81.2),
                (last.isoformat(), 79.7)]
        with mock.patch.object(sources, "_get", return_value=_csv(rows)):
            hist = sources._fred_quote_history("cl.f", days=30)
        self.assertEqual(hist[-1][1], 79.7)
        self.assertEqual(sources._LAST_SOURCE["cl.f"], ("fred:POILWTIUSDM", 120))
        quote = sources.quote_from_history("WTI", hist, symbol="cl.f")
        self.assertFalse(quote["stale"])

    def test_brent_uses_monthly_series_when_daily_404s(self):
        last = date.today() - timedelta(days=99)
        monthly_rows = [((last - timedelta(days=31)).isoformat(), 84.5),
                        (last.isoformat(), 83.7)]
        requested = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            sid = url.split("id=", 1)[1].split("&", 1)[0]
            requested.append(sid)
            if sid == "DCOILBRENTEU":
                raise RuntimeError("curl: (22) The requested URL returned error: 404 HTTP 404")
            if sid == "POILBREUSDM":
                return _csv(monthly_rows)
            raise AssertionError(f"unexpected FRED series {sid}")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            hist = sources._fred_quote_history("cb.f", days=30)
        self.assertEqual(hist[-1][1], 83.7)
        self.assertEqual(requested, ["DCOILBRENTEU", "DCOILBRENTEU", "POILBREUSDM"])
        self.assertEqual(sources._LAST_SOURCE["cb.f"], ("fred:POILBREUSDM", 120))
        quote = sources.quote_from_history("Brent", hist, symbol="cb.f")
        self.assertFalse(quote["stale"])


class TestStooq(TransportCase):
    def test_timeouts_cool_every_host_so_the_next_symbol_is_free(self):
        calls = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            calls.append(url)
            raise RuntimeError("curl: (28) Failed to connect to stooq.pl port 443 after 5000 ms")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            with self.assertRaises(RuntimeError):
                sources._stooq_raw_history("^dax", days=30)
            first_round = len(calls)
            self.assertGreaterEqual(first_round, 2)

            calls.clear()
            with self.assertRaises(RuntimeError):
                sources._stooq_raw_history("^hsi", days=30)
            self.assertEqual(calls, [], "dead stooq hosts must not be retried per symbol")


class TestCascade(TransportCase):
    def test_falls_through_to_fred_when_yahoo_is_rate_limited(self):
        def fake_get(url, timeout=None, headers=None, **kwargs):
            if "finance.yahoo.com" in url:
                raise RuntimeError("curl: (22) ... error: 429 HTTP 429")
            if "fredgraph.csv" in url:
                return _csv(_recent(4))
            raise AssertionError(f"unexpected url {url}")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            hist = sources.stooq_history("^spx", days=30)
        self.assertEqual(len(hist), 4)
        self.assertTrue(sources._LAST_SOURCE["^spx"][0].startswith("fred:SP500"))

    def test_fred_only_aluminum_card_never_sends_unverified_yahoo_or_stooq_tickers(self):
        last = date.today() - timedelta(days=99)
        rows = [((last - timedelta(days=31)).isoformat(), 3200.0),
                (last.isoformat(), 3158.3)]
        calls = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            calls.append(url)
            self.assertIn("id=PALUMUSDM", url)
            return _csv(rows)

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            hist = sources.stooq_history("al.f", days=30)
        self.assertEqual(hist[-1][1], 3158.3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sources._LAST_SOURCE["al.f"], ("fred:PALUMUSDM", 120))

    def test_quote_dict_flags_stale_data(self):
        old = (date.today() - timedelta(days=40)).isoformat()
        q = sources.quote_from_history("Copper", [(old, 9000.0), (old, 9100.0)],
                                       source="fred:PCOPPUSDM", max_age_days=120,
                                       symbol="hg.f")
        self.assertFalse(q["stale"])
        ancient = (date.today() - timedelta(days=400)).isoformat()
        q2 = sources.quote_from_history("Copper", [(ancient, 9000.0), (ancient, 9100.0)],
                                        source="fred:PCOPPUSDM", max_age_days=120,
                                        symbol="hg.f")
        self.assertTrue(q2["stale"])

    def test_all_sources_failing_raises_a_combined_error(self):
        def fake_get(url, timeout=None, headers=None, **kwargs):
            raise RuntimeError("curl: (35) SSL_ERROR_SYSCALL")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            with self.assertRaises(RuntimeError) as ctx:
                sources.stooq_history("^dax", days=30)
        self.assertIn("all sources failed", str(ctx.exception))


class TestUSTreasuryErrors(TransportCase):
    def test_breakeven_accepts_both_10yr_header_spellings(self):
        observed = (date.today() - timedelta(days=1)).strftime("%m/%d/%Y")

        def fake_year(year, kind="daily_treasury_yield_curve"):
            if kind == "daily_treasury_real_yield_curve":
                # Real-yield export has appeared with an uppercase YR header.
                return [{"Date": observed, "10 YR": "1.75"}]
            return [{"Date": observed, "10 Yr": "4.00"}]

        with mock.patch.object(sources, "_ustreasury_csv_year", side_effect=fake_year):
            series = sources._ustreasury_breakeven_series(years=1)
        self.assertEqual(series, [(sources._parse_us_date(observed), 2.25)])

    def test_underlying_cause_is_surfaced(self):
        def boom(year, kind="daily_treasury_yield_curve"):
            raise RuntimeError("curl: (35) SSL_ERROR_SYSCALL")

        with mock.patch.object(sources, "_ustreasury_csv_year", side_effect=boom):
            with self.assertRaises(RuntimeError) as ctx:
                sources._ustreasury_curve_series(years=1)
        self.assertIn("SSL_ERROR_SYSCALL", str(ctx.exception))

        with mock.patch.object(sources, "_ustreasury_csv_year", side_effect=boom):
            with self.assertRaises(RuntimeError) as ctx:
                sources._ustreasury_breakeven_series(years=1)
        self.assertIn("SSL_ERROR_SYSCALL", str(ctx.exception))


class TestPreFlight(TransportCase):
    def test_warm_providers_trips_dead_stooq_hosts(self):
        calls = []

        def fake_get(url, timeout=None, headers=None, **kwargs):
            calls.append(url)
            raise RuntimeError("curl: (28) Failed to connect to stooq.pl port 443 "
                               "after 3000 ms: Timeout was reached")

        with mock.patch.object(sources, "_get", side_effect=fake_get):
            sources.warm_providers(probe_timeout=3)
            self.assertEqual(len(calls), len(sources.STOOQ_ENDPOINTS),
                             "one probe per stooq endpoint")
            calls.clear()
            with self.assertRaises(RuntimeError):
                sources._stooq_raw_history("^dax", days=30)
            self.assertEqual(calls, [], "probed-dead hosts must cost the fan-out nothing")

    def test_warm_all_runs_preflight_for_market_symbols(self):
        import app

        seen = []

        class FakeBackend:
            @staticmethod
            def warm_providers():
                seen.append("warm_providers")

            @staticmethod
            def prefetch_yahoo(symbols, days=150):
                seen.append(("prefetch_yahoo", len(symbols), days))

        expected = len(app.config.INDICES) + len(app.config.COMMODITIES)
        with mock.patch.object(app.config, "DEMO", False), \
                mock.patch.object(app, "backend", FakeBackend), \
                mock.patch.object(app, "fetch_into_cache", lambda *a, **k: None):
            app.warm_all(force=True)

        self.assertEqual(seen[0], "warm_providers")
        self.assertEqual(seen[1], ("prefetch_yahoo", expected, app.config.SPARK_DAYS))


class TestRendering(TransportCase):
    def test_footer_shows_provider_health(self):
        import render

        page = render.build_page({
            "indices": [], "commodities": [], "fx": [], "cpi": [], "gdp": [],
            "policy_pairs": [], "policy_series": {},
            "curve": [], "curve_meta": ("T10Y2Y", "spread", 4),
            "breakeven": [], "breakeven_meta": ("T10YIE", "breakeven"),
            "errors": ["stooq:^dax: all sources failed"],
            "providers": [{"provider": "yahoo", "state": "cooling", "fails": 3,
                           "reason": "curl: (22) error: 429", "retry_in": 540}],
        }, demo=False)
        self.assertIn("upstream health", page)
        self.assertIn("yahoo", page)
        self.assertIn("source issues", page)

    def test_summary_carries_provider_status(self):
        import app

        summary = app.summary()
        self.assertIn("providers", summary)
        self.assertIn("errors", summary)
        import render
        page = render.build_page(summary, demo=True)
        self.assertIn("ECON", page)


if __name__ == "__main__":
    unittest.main()
