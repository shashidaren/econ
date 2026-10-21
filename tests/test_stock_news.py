"""Offline tests for the Stocks-tab headlines and seven-day price movement.

No network: every headline feed is faked at the transport seam
(`sources._get`, injected into `news.fetch_news`). The real cascade, parsers,
filters, breakers and renderer run.

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "dashboard"))

import news  # noqa: E402
import render  # noqa: E402
import sources  # noqa: E402
import stocks  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)

GOOGLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>Pfizer - Google News</title>
  <item>
    <title>Pfizer lifts full-year guidance after trial data - Reuters</title>
    <link>https://news.google.com/rss/articles/AAA</link>
    <guid isPermaLink="false">AAA</guid>
    <pubDate>Tue, 20 Oct 2026 08:00:00 GMT</pubDate>
    <description>Pfizer said ...</description>
    <source url="https://www.reuters.com">Reuters</source>
  </item>
  <item>
    <title>Pfizer shares rise in premarket trading - Bloomberg</title>
    <link>https://news.google.com/rss/articles/BBB</link>
    <pubDate>Mon, 19 Oct 2026 22:30:00 GMT</pubDate>
    <source url="https://www.bloomberg.com">Bloomberg</source>
  </item>
  <item>
    <title>Pfizer and BioNTech update vaccine label - Reuters</title>
    <link>https://news.google.com/rss/articles/CCC</link>
    <pubDate>Sun, 18 Oct 2026 09:15:00 GMT</pubDate>
    <source url="https://www.reuters.com">Reuters</source>
  </item>
</channel>
</rss>"""

BING_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Bristol Myers Squibb wins approval for new therapy - AP</title>
    <link>https://www.bing.com/news/apiclick.aspx?ref=xyz</link>
    <pubDate>Tue, 20 Oct 2026 06:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM_FEED = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Pfizer announces manufacturing investment</title>
    <link href="https://example.com/pfizer-investment"/>
    <updated>2026-10-19T10:00:00Z</updated>
    <author><name>Example News</name></author>
  </entry>
</feed>"""


def _getter(mapping):
    """Return a fake transport keyed by URL substring; unknown URLs raise."""
    calls = []

    def fake(url, timeout=None, connect_timeout=None, **kwargs):
        calls.append(url)
        for needle, body in mapping.items():
            if needle in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise RuntimeError(f"HTTP 500 unexpected host: {url}")

    fake.calls = calls
    return fake


CANDIDATE = {"symbol": "PFE", "name": "Pfizer"}


class FeedParsingTests(unittest.TestCase):
    def test_rss_items_keep_title_link_date_and_publisher(self):
        items = news.parse_feed(GOOGLE_RSS)
        self.assertEqual(len(items), 3)
        first = items[0]
        self.assertEqual(first["title"],
                         "Pfizer lifts full-year guidance after trial data - Reuters")
        self.assertEqual(first["link"], "https://news.google.com/rss/articles/AAA")
        self.assertEqual(first["publisher"], "Reuters")
        self.assertEqual(first["published"],
                         datetime(2026, 10, 20, 8, 0, tzinfo=timezone.utc))

    def test_atom_entries_are_parsed_with_href_and_updated(self):
        items = news.parse_feed(ATOM_FEED)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["link"], "https://example.com/pfizer-investment")
        self.assertEqual(items[0]["published"],
                         datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc))

    def test_malformed_feed_is_empty_not_an_exception(self):
        self.assertEqual(news.parse_feed("<rss><channel><item>"), [])
        self.assertEqual(news.parse_feed("not xml at all"), [])

    def test_publisher_suffix_is_removed_from_the_title(self):
        self.assertEqual(
            news.clean_title("Pfizer lifts guidance - Reuters", "Reuters"),
            "Pfizer lifts guidance")
        # A publisher that does not match the suffix must not chop the title.
        self.assertEqual(
            news.clean_title("Pfizer lifts guidance - Reuters", "Bloomberg"),
            "Pfizer lifts guidance - Reuters")


class RelevanceTests(unittest.TestCase):
    def test_ticker_company_name_and_distinctive_token_all_match(self):
        self.assertTrue(news.is_relevant("PFE wins a ruling", "PFE", "Pfizer"))
        self.assertTrue(news.is_relevant("Pfizer wins a ruling", "PFE", "Pfizer"))
        self.assertTrue(news.is_relevant("Bristol Myers Squibb cuts costs",
                                         "BMY", "Bristol Myers Squibb"))
        self.assertTrue(news.is_relevant("What is next for AT&T?", "T", "AT&T"))

    def test_unrelated_headline_is_rejected(self):
        self.assertFalse(news.is_relevant("Oil prices surge on supply news",
                                          "PFE", "Pfizer"))
        # Single-letter tickers must not match by themselves.
        self.assertFalse(news.is_relevant("T-Mobile raises prices", "T", "AT&T"))


class FetchNewsTests(unittest.TestCase):
    def setUp(self):
        sources.reset_breakers()

    def tearDown(self):
        sources.reset_breakers()

    def test_recent_relevant_unduplicated_items_are_kept_newest_first(self):
        body = GOOGLE_RSS.replace(
            "</channel>",
            """  <item>
    <title>Pfizer lifts full-year guidance after trial data - Reuters</title>
    <link>https://news.google.com/rss/articles/AAAA</link>
    <pubDate>Tue, 20 Oct 2026 07:00:00 GMT</pubDate>
    <source url="https://www.reuters.com">Reuters</source>
  </item>
  <item>
    <title>Pfizer guidance cut in September - old story</title>
    <link>https://news.google.com/rss/articles/OLD</link>
    <pubDate>Tue, 15 Sep 2026 07:00:00 GMT</pubDate>
    <source url="https://example.com">Example</source>
  </item>
  <item>
    <title>Oil prices surge on supply news - Reuters</title>
    <link>https://news.google.com/rss/articles/OIL</link>
    <pubDate>Tue, 20 Oct 2026 09:00:00 GMT</pubDate>
    <source url="https://www.reuters.com">Reuters</source>
  </item>
</channel>""")
        getter = _getter({"news.google.com": body})
        result = news.fetch_news([CANDIDATE], now=NOW, getter=getter)

        self.assertEqual(result["status"], "ok")
        self.assertEqual([i["title"] for i in result["items"]["PFE"]], [
            "Pfizer lifts full-year guidance after trial data",
            "Pfizer shares rise in premarket trading",
            "Pfizer and BioNTech update vaccine label",
        ])
        self.assertEqual(result["coverage"]["with_headlines"], 1)
        self.assertEqual(result["coverage"]["providers"], ["Google News RSS"])

    def test_old_undated_and_off_topic_items_are_dropped(self):
        body = """<rss><channel>
          <item><title>Pfizer old news story from August</title>
            <pubDate>Mon, 03 Aug 2026 10:00:00 GMT</pubDate>
            <link>https://example.com/1</link></item>
          <item><title>Pfizer statement without any date</title>
            <link>https://example.com/2</link></item>
          <item><title>Copper rallies on demand hopes</title>
            <pubDate>Tue, 20 Oct 2026 09:00:00 GMT</pubDate>
            <link>https://example.com/3</link></item>
          <item><title>Short</title>
            <pubDate>Tue, 20 Oct 2026 09:00:00 GMT</pubDate></item>
        </channel></rss>"""
        getter = _getter({"news.google.com": body, "bing.com": body,
                          "feeds.finance.yahoo.com": body})
        result = news.fetch_news([CANDIDATE], now=NOW, getter=getter)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], {})
        self.assertIn("none inside the 7-day window", result["errors"]["PFE"])

    def test_bing_is_used_when_google_fails(self):
        getter = _getter({
            "news.google.com": RuntimeError("HTTP 503"),
            "bing.com": BING_RSS,
        })
        result = news.fetch_news(
            [{"symbol": "BMY", "name": "Bristol Myers Squibb"}],
            now=NOW, getter=getter)
        self.assertEqual(result["status"], "ok")
        item = result["items"]["BMY"][0]
        # Bing does not expose a publisher tag, so the " - AP" suffix stays;
        # the publisher column names the feed host instead of inventing AP.
        self.assertEqual(item["title"],
                         "Bristol Myers Squibb wins approval for new therapy - AP")
        self.assertEqual(item["publisher"], "Bing News")
        self.assertIn("Google News RSS: HTTP 503", result["errors"]["BMY"])

    def test_yahoo_is_the_last_resort(self):
        yahoo_rss = """<rss><channel><item>
            <title>Pfizer declares quarterly dividend</title>
            <link>https://finance.yahoo.com/news/pfizer-dividend</link>
            <pubDate>Tue, 20 Oct 2026 05:00:00 GMT</pubDate>
          </item></channel></rss>"""
        getter = _getter({
            "news.google.com": RuntimeError("HTTP 500"),
            "bing.com": RuntimeError("HTTP 500"),
            "feeds.finance.yahoo.com": yahoo_rss,
        })
        result = news.fetch_news([CANDIDATE], now=NOW, getter=getter)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"]["providers"], ["Yahoo Finance RSS"])

    def test_all_feeds_failing_returns_a_visible_status_not_an_exception(self):
        getter = _getter({
            "news.google.com": RuntimeError("HTTP 500"),
            "bing.com": RuntimeError("HTTP 500"),
            "feeds.finance.yahoo.com": RuntimeError("HTTP 500"),
        })
        result = news.fetch_news([CANDIDATE], now=NOW, getter=getter)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"], {})
        self.assertIn("PFE", result["errors"])
        self.assertIn("Google News RSS", result["errors"]["PFE"])
        self.assertIn("Yahoo Finance RSS", result["errors"]["PFE"])
        self.assertTrue(result["message"])

    def test_cooling_provider_is_skipped_without_a_request(self):
        sources.note_provider_failure("google_news", "HTTP 429", rate_limited=True)
        getter = _getter({"bing.com": BING_RSS})
        result = news.fetch_news(
            [{"symbol": "BMY", "name": "Bristol Myers Squibb"}],
            now=NOW, getter=getter)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(any("news.google.com" in url for url in getter.calls))
        self.assertIn("cooling down", result["errors"]["BMY"])

    def test_empty_candidate_list_is_handled(self):
        getter = _getter({})
        result = news.fetch_news([], now=NOW, getter=getter)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(getter.calls, [])

    def test_duplicate_symbols_are_fetched_once(self):
        getter = _getter({"news.google.com": GOOGLE_RSS})
        result = news.fetch_news([CANDIDATE, dict(CANDIDATE)], now=NOW, getter=getter)
        self.assertEqual(len(getter.calls), 1)
        self.assertEqual(result["coverage"]["symbols"], 1)

    def test_per_symbol_limit_is_honoured(self):
        getter = _getter({"news.google.com": GOOGLE_RSS})
        result = news.fetch_news([CANDIDATE], now=NOW, per_symbol=1, getter=getter)
        self.assertEqual(len(result["items"]["PFE"]), 1)

    def test_javascript_links_are_not_passed_through(self):
        body = """<rss><channel><item>
            <title>Pfizer alert with a hostile link</title>
            <link>javascript:alert(1)</link>
            <pubDate>Tue, 20 Oct 2026 08:00:00 GMT</pubDate>
          </item></channel></rss>"""
        getter = _getter({"news.google.com": body})
        result = news.fetch_news([CANDIDATE], now=NOW, getter=getter)
        self.assertIsNone(result["items"]["PFE"][0]["link"])

    def test_sources_wrapper_delegates_to_the_news_module(self):
        with mock.patch("news.fetch_news",
                        return_value={"status": "ok"}) as patched:
            result = sources.fetch_news([CANDIDATE], days=7, per_symbol=2)
        self.assertEqual(result, {"status": "ok"})
        patched.assert_called_once_with([CANDIDATE], days=7, per_symbol=2)


class SevenDayMovementTests(unittest.TestCase):
    @staticmethod
    def _bars(closes, end=date(2026, 10, 16)):
        """Daily bars ending at `end`, one per calendar day."""
        return [((end - timedelta(days=len(closes) - 1 - i)).isoformat(),
                 float(close), 1_000_000) for i, close in enumerate(closes)]

    def test_ret7_uses_the_last_close_at_or_before_seven_days_ago(self):
        # 10-09 is the last bar on/before 10-16 minus 7 days; it is the base.
        closes = [100.0] * 59 + [102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 110.0]
        metrics = stocks._metrics(self._bars(closes))
        self.assertEqual(metrics["week_start"], "2026-10-09")
        self.assertEqual(metrics["week_end"], "2026-10-16")
        self.assertAlmostEqual(metrics["ret7_pct"], (110.0 / 102.0 - 1) * 100, 6)
        self.assertEqual(metrics["week_high"], 110.0)
        self.assertEqual(metrics["week_low"], 102.0)
        self.assertEqual(len(metrics["recent_bars"]), 12)
        self.assertEqual(metrics["recent_bars"][-1], ["2026-10-16", 110.0])

    def test_screen_reports_seven_day_breadth_of_displayed_candidates(self):
        # A strong prior uptrend keeps every variant above its 20-day average,
        # so the week summary is measured on names that really passed the screen.
        base = [15.0] * 48 + [15.0 * (1.015 ** i) for i in range(1, 13)]
        week_tail = [18.5, 18.8, 19.1, 19.4, 19.6, 19.8, 20.0]

        def candidate(final):
            return {"rows": self._bars(base + week_tail + [final]),
                    "source": "test"}

        universe = [(sym, f"{sym} Corp", "Tech", f"us{sym}.N")
                    for sym in ("AAA", "BBB", "CCC")]
        data = {
            "AAA": candidate(22.0),      # +18.9% over the week
            "BBB": candidate(18.2),      # -1.6% over the week
            "CCC": candidate(18.5),      # unchanged over the week
        }
        screen = stocks.build_screen(universe, data, as_of=date(2026, 10, 16))
        self.assertEqual(screen["eligible_count"], 3)
        week = screen["week"]
        self.assertEqual(week["count"], 3)
        self.assertEqual(week["up"], 1)
        self.assertEqual(week["down"], 1)
        self.assertEqual(week["flat"], 1)
        self.assertAlmostEqual(
            week["average_ret7_pct"],
            round(sum(c["ret7_pct"] for c in screen["candidates"]) / 3, 2),
            places=2)
        self.assertEqual(week["best"]["symbol"], "AAA")
        self.assertEqual(week["worst"]["symbol"], "BBB")
        self.assertEqual(week["end_date"], "2026-10-16")
        self.assertEqual(week["start_date"], "2026-10-09")

    def test_week_summary_is_none_without_candidates(self):
        screen = stocks.build_screen([], {}, as_of=date(2026, 10, 16))
        self.assertIsNone(screen["week"])


SCREEN_WITH_WEEK_AND_NEWS = {
    "status": "ok", "message": "2 eligible", "universe_size": 2,
    "quote_count": 2, "eligible_count": 2, "max_candidates": 7,
    "data_date": "2026-10-16", "source": "test",
    "filters": {"price_min": 5, "price_max_exclusive": 50,
                "min_avg_dollar_volume": 5_000_000, "minimum_sessions": 61},
    "exclusions": {},
    "week": {
        "window_days": 7, "end_date": "2026-10-16", "start_date": "2026-10-09",
        "count": 2, "average_ret7_pct": 3.4, "up": 1, "down": 1, "flat": 0,
        "best": {"symbol": "AAA", "ret7_pct": 9.1},
        "worst": {"symbol": "BBB", "ret7_pct": -2.3},
    },
    "candidates": [
        {"symbol": "AAA", "name": "Alpha Corp", "sector": "Tech", "close": 20.0,
         "risk": "Medium", "ret1_pct": 0.5, "ret7_pct": 9.1, "ret5_pct": 2.0,
         "ret20_pct": 4.0, "ret60_pct": 6.0, "vol20_annual_pct": 24.0,
         "drawdown60_pct": -4.0, "avg_dollar_volume20": 12_000_000,
         "rank_score": 3.2, "as_of": "2026-10-16", "source": "test",
         "quote_age_days": 0, "reasons": ["above 20-day average"],
         "week_high": 21.0, "week_low": 18.2,
         "recent_bars": [["2026-10-14", 18.5], ["2026-10-15", 19.4],
                         ["2026-10-16", 20.0]]},
        {"symbol": "BBB", "name": "Beta Corp", "sector": "Tech", "close": 30.0,
         "risk": "High", "ret1_pct": -1.0, "ret7_pct": -2.3, "ret5_pct": -1.0,
         "ret20_pct": 1.0, "ret60_pct": 2.0, "vol20_annual_pct": 61.0,
         "drawdown60_pct": -25.0, "avg_dollar_volume20": 8_000_000,
         "rank_score": 0.4, "as_of": "2026-10-16", "source": "test",
         "quote_age_days": 0, "reasons": [],
         "week_high": 31.0, "week_low": 29.0,
         "recent_bars": [["2026-10-14", 30.6], ["2026-10-15", 30.3],
                         ["2026-10-16", 30.0]]},
    ],
    "news": {
        "status": "partial",
        "message": "Recent headlines found for 1 of 2 displayed stock(s).",
        "window_days": 7,
        "fetched_at_utc": "2026-10-20T12:00:00+00:00",
        "coverage": {"symbols": 2, "with_headlines": 1, "headlines": 1,
                     "providers": ["Google News RSS"]},
        "items": {"AAA": [{
            "title": "Alpha Corp beats estimates",
            "link": "https://example.com/alpha",
            "publisher": "Example Wire",
            "published_utc": "2026-10-20T09:00:00+00:00",
        }]},
        "errors": {"BBB": "Google News RSS: no 7-day match"},
    },
}


class StocksPageNewsTests(unittest.TestCase):
    def test_page_shows_week_strip_sparkline_and_linked_headlines(self):
        page = render.build_stocks_page(SCREEN_WITH_WEEK_AND_NEWS,
                                        generated_at=NOW)
        self.assertIn("7-day price movement", page)
        self.assertIn("2026-10-09 → 2026-10-16", page)
        self.assertIn("Average 7-day move", page)
        self.assertIn("+3.4%", page)
        self.assertIn("Up / down", page)
        self.assertIn("1 / 1", page)
        self.assertIn("AAA", page)
        self.assertIn("+9.1%", page)
        self.assertIn("stock-spark", page)
        self.assertIn("7-day range $18.20–$21.00", page)
        self.assertIn("Recent headlines", page)
        self.assertIn("Alpha Corp beats estimates", page)
        self.assertIn('href="https://example.com/alpha"', page)
        self.assertIn('rel="noopener noreferrer"', page)
        self.assertIn("Example Wire", page)
        self.assertIn("3 h ago", page)
        self.assertIn("No headline inside the last 7 days matched this ticker",
                      page)
        self.assertIn("Headlines: Recent headlines found for 1 of 2", page)
        self.assertIn("never used in the ranking", page)

    def test_page_escapes_headlines_and_drops_unsafe_links(self):
        screen = dict(SCREEN_WITH_WEEK_AND_NEWS)
        screen["news"] = {
            "status": "ok", "message": "1 of 1",
            "window_days": 7, "fetched_at_utc": "2026-10-20T12:00:00+00:00",
            "coverage": {"symbols": 1, "with_headlines": 1, "headlines": 1,
                         "providers": ["Google News RSS"]},
            "items": {"AAA": [{
                "title": "Alpha <script>alert(1)</script> story",
                "link": "javascript:alert(1)",
                "publisher": "<b>Bad Wire</b>",
                "published_utc": "2026-10-20T09:00:00+00:00",
            }]},
            "errors": {},
        }
        screen["candidates"] = screen["candidates"][:1]
        page = render.build_stocks_page(screen, generated_at=NOW)
        self.assertNotIn("<script>", page)
        self.assertNotIn("javascript:", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;b&gt;Bad Wire&lt;/b&gt;", page)

    def test_page_reports_a_news_outage_without_hiding_the_screen(self):
        screen = dict(SCREEN_WITH_WEEK_AND_NEWS)
        screen["news"] = {"status": "unavailable",
                          "message": "Headline feeds returned nothing usable.",
                          "items": {}, "errors": {}}
        page = render.build_stocks_page(screen, generated_at=NOW)
        self.assertIn("Headlines unavailable", page)
        self.assertIn("Headline feeds returned nothing usable.", page)
        self.assertIn("Quotes and the stock screen are unaffected", page)
        self.assertEqual(page.count('class="stock-card"'), 2)

    def test_disabled_news_is_stated_on_the_page(self):
        screen = dict(SCREEN_WITH_WEEK_AND_NEWS)
        screen["news"] = {"status": "disabled",
                          "message": "Stock headlines are disabled (ECON_STOCK_NEWS=0).",
                          "items": {}, "errors": {}}
        page = render.build_stocks_page(screen, generated_at=NOW)
        self.assertIn("ECON_STOCK_NEWS=0", page)
        self.assertNotIn("Recent headlines", page)

    def test_page_without_news_or_week_data_still_renders(self):
        screen = dict(SCREEN_WITH_WEEK_AND_NEWS)
        screen.pop("news")
        screen.pop("week")
        for candidate in screen["candidates"]:
            for key in ("ret7_pct", "recent_bars", "week_high", "week_low"):
                candidate.pop(key, None)
        page = render.build_stocks_page(screen, generated_at=NOW)
        self.assertEqual(page.count('class="stock-card"'), 2)
        self.assertNotIn("7-day price movement", page)
        self.assertNotIn("Recent headlines", page)
        self.assertIn("7 days", page)          # stat label degrades to a dash


class DemoTests(unittest.TestCase):
    def test_demo_headlines_are_labelled_and_carry_no_links(self):
        import demo

        with mock.patch("sources._get",
                        side_effect=AssertionError("demo must not use the network")):
            result = demo.fetch_news([CANDIDATE], per_symbol=2,
                                     now=NOW)
        self.assertEqual(result["status"], "ok")
        items = result["items"]["PFE"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["title"].startswith("DEMO headline") for item in items))
        self.assertTrue(all(item["link"] is None for item in items))
        self.assertEqual(items[0]["publisher"], "DEMO synthetic")

    def test_config_defaults_match_the_documented_window(self):
        import app

        self.assertTrue(app.config.STOCK_NEWS_ENABLED)
        self.assertEqual(app.config.STOCK_NEWS_WINDOW_DAYS, 7)
        self.assertEqual(app.config.STOCK_NEWS_PER_SYMBOL, 3)


if __name__ == "__main__":
    unittest.main()
