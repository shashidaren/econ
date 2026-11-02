"""Offline Telegram alert tests. No request ever reaches api.telegram.org."""

from __future__ import annotations

import copy
import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dashboard"))

import alerts
import app
import config
import render

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)  # Monday, regular U.S. session
TOKEN = "123456:TEST_ONLY_NOT_A_REAL_TOKEN"
CHAT = "-1001234567890"


def fixture(change=3.0, symbol="PFE", *, now=NOW):
    screen = {"status": "ok", "candidates": [{"symbol": symbol, "name": "Pfizer", "close": 25.8}]}
    entry = {
        "at": now.timestamp() - 5,
        "err": None,
        "data": {
            "status": "ok",
            "fetched_at_utc": (now - timedelta(seconds=5)).isoformat(),
            "market_session": {"state": "regular"},
            "items": {symbol: {
                "symbol": symbol,
                "price": 26.574,
                "prev_close": 25.8,
                "change_pct": change,
                "as_of_utc": (now - timedelta(minutes=15)).isoformat(),
                "source": "Sina US quote",
                "delayed": True,
            }},
        },
    }
    return screen, entry


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def response(payload):
    return _Response(json.dumps(payload).encode())


class AlertTestCase(unittest.TestCase):
    def setUp(self):
        self.values = {
            "DEMO": False, "STOCK_QUOTES_ENABLED": True,
            "TELEGRAM_ENABLED": True, "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_CHAT_ID": CHAT, "STOCK_ALERT_THRESHOLD_PCT": 3.0,
            "TELEGRAM_MAX_QUOTE_AGE": 1800,
        }
        self.patches = [mock.patch.object(config, k, v) for k, v in self.values.items()]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = str(Path(self.tmp.name) / "state.json")
        patch = mock.patch.object(config, "TELEGRAM_STATE_FILE", self.state_path)
        patch.start()
        self.addCleanup(patch.stop)
        self.notifier = alerts.StockAlertNotifier()


class EligibilityTests(AlertTestCase):
    def test_threshold_is_inclusive_and_only_displayed_symbols_are_eligible(self):
        screen, entry = fixture()
        entry["data"]["items"]["HIDDEN"] = {
            **entry["data"]["items"]["PFE"], "symbol": "HIDDEN", "change_pct": 20,
        }
        events = alerts.eligible_movements(screen, entry, now=NOW)
        self.assertEqual([e["symbol"] for e in events], ["PFE"])
        self.assertEqual(events[0]["session"], "2026-11-02")
        self.assertEqual(events[0]["change_pct"], 3.0)
        text = alerts.format_messages(events)[0]
        for expected in ("PFE", "+3.00%", "$26.57", "$25.80", "22:45 MYT",
                         "Sina US quote", "~15 minutes delayed", "not a buy signal"):
            self.assertIn(expected, text)

    def test_below_threshold_and_falls_do_not_notify(self):
        for change in (2.99, 0, -3.0, -10):
            with self.subTest(change=change):
                screen, entry = fixture(change)
                self.assertEqual(alerts.eligible_movements(screen, entry, now=NOW), [])

    def test_invalid_nonfinite_and_undated_quotes_are_rejected(self):
        for field, values in {
            "change_pct": (None, "bad", float("nan"), float("inf")),
            "price": (None, 0, -1, float("inf")),
            "as_of_utc": (None, "not a timestamp"),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    screen, entry = fixture()
                    entry["data"]["items"]["PFE"][field] = value
                    self.assertEqual(alerts.eligible_movements(screen, entry, now=NOW), [])

    def test_stale_or_future_quotes_and_old_batch_are_rejected(self):
        for minutes in (31, -3):
            screen, entry = fixture()
            entry["data"]["items"]["PFE"]["as_of_utc"] = (NOW - timedelta(minutes=minutes)).isoformat()
            self.assertEqual(alerts.eligible_movements(screen, entry, now=NOW), [])
        screen, entry = fixture()
        entry["at"] -= 3600
        self.assertEqual(alerts.eligible_movements(screen, entry, now=NOW), [])
        screen, entry = fixture()
        entry["data"]["fetched_at_utc"] = (NOW - timedelta(hours=1)).isoformat()
        self.assertEqual(alerts.eligible_movements(screen, entry, now=NOW), [])

    def test_cached_outage_demo_disabled_and_extended_session_are_rejected(self):
        mutations = [
            lambda s, e: e.update(err="provider unavailable"),
            lambda s, e: e["data"].update(status="cached", served_from_cache=True),
            lambda s, e: e["data"].update(status="unavailable"),
            lambda s, e: e["data"].update(demo=True),
            lambda s, e: s.update(demo=True),
            lambda s, e: e["data"].update(market_session={"state": "after_hours"}),
            lambda s, e: e["data"]["items"]["PFE"].update(demo=True),
        ]
        for mutate in mutations:
            screen, entry = fixture()
            mutate(screen, entry)
            self.assertEqual(alerts.eligible_movements(screen, entry, now=NOW), [])
        for key, value in (("DEMO", True), ("STOCK_QUOTES_ENABLED", False)):
            with mock.patch.object(config, key, value):
                self.assertEqual(alerts.eligible_movements(*fixture(), now=NOW), [])

    def test_extended_hours_percentage_is_not_substituted(self):
        screen, entry = fixture(1.0)
        entry["data"]["items"]["PFE"]["extended_change_pct"] = 8.0
        self.assertEqual(alerts.eligible_movements(screen, entry, now=NOW), [])

    def test_message_batches_stay_below_limit_and_treat_names_as_plain_text(self):
        screen, entry = fixture()
        events = alerts.eligible_movements(screen, entry, now=NOW)
        events = [{**events[0], "symbol": f"T{i}", "name": "<b>*not markup*</b>" * 8}
                  for i in range(32)]
        messages = alerts.format_messages(events)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(alerts._message_length(text) <= 4096 for text in messages))
        self.assertEqual(sum(text.count("vs previous close\n") for text in messages), 32)
        self.assertIn("<b>*not markup*</b>", messages[0])
        emoji_events = [{**events[0], "name": "📈" * 100} for _ in range(32)]
        self.assertTrue(all(alerts._message_length(text) <= 4096
                            for text in alerts.format_messages(emoji_events)))


class DeliveryTests(AlertTestCase):
    def test_success_dedupes_across_refreshes_restarts_and_token_rotation(self):
        data = fixture()
        with mock.patch.object(alerts, "send_message", return_value=10) as send:
            self.assertEqual(self.notifier.process(*data, now=NOW)["status"], "sent")
            self.assertEqual(self.notifier.process(*data, now=NOW)["status"], "already_sent")
            restarted = alerts.StockAlertNotifier()
            self.assertEqual(restarted.process(*data, now=NOW)["status"], "already_sent")
            with mock.patch.object(config, "TELEGRAM_BOT_TOKEN", "654321:ROTATED_TEST_TOKEN"):
                self.assertEqual(restarted.process(*data, now=NOW)["status"], "already_sent")
            self.assertEqual(send.call_count, 1)
            saved = Path(self.state_path).read_text()
            self.assertNotIn(TOKEN, saved)
            self.assertNotIn(CHAT, saved)
            self.assertEqual(stat.S_IMODE(Path(self.state_path).stat().st_mode), 0o600)
            tomorrow = NOW + timedelta(days=1)
            next_day = restarted.process(*fixture(now=tomorrow), now=tomorrow)
            self.assertEqual(next_day["status"], "sent")
            self.assertEqual(send.call_count, 2)

    def test_different_chat_gets_its_own_dedupe(self):
        with mock.patch.object(alerts, "send_message", return_value=10) as send:
            self.notifier.process(*fixture(), now=NOW)
            with mock.patch.object(config, "TELEGRAM_CHAT_ID", "12345678"):
                self.assertEqual(self.notifier.process(*fixture(), now=NOW)["status"], "sent")
            self.assertEqual(send.call_count, 2)
            self.assertEqual(self.notifier.process(*fixture(), now=NOW)["status"], "already_sent")
            self.assertEqual(send.call_count, 2)

    def test_failure_does_not_mark_delivered_and_cooldown_prevents_hammering(self):
        with mock.patch.object(alerts, "send_message",
                               side_effect=alerts.TelegramError("safe failure", status=503)) as send:
            result = self.notifier.process(*fixture(), now=NOW)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(self.notifier.process(*fixture(), now=NOW + timedelta(seconds=30))["status"],
                             "cooldown")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(self.notifier.process(*fixture(), now=NOW + timedelta(seconds=61))["status"],
                             "failed")
            self.assertEqual(send.call_count, 2)
            self.assertEqual(json.loads(Path(self.state_path).read_text())["destinations"]
                             [self.notifier._destination_key()]["sent"], {})

    def test_telegram_retry_after_is_persisted(self):
        error = alerts.TelegramError("Telegram HTTP 429: rate limited.", status=429, retry_after=120)
        with mock.patch.object(alerts, "send_message", side_effect=error) as send:
            self.notifier.process(*fixture(), now=NOW)
            restarted = alerts.StockAlertNotifier()
            self.assertEqual(restarted.process(*fixture(), now=NOW + timedelta(seconds=90))["status"],
                             "cooldown")
            self.assertEqual(send.call_count, 1)
            self.assertEqual(restarted.process(*fixture(), now=NOW + timedelta(seconds=121))["status"],
                             "failed")

    def test_partial_batch_delivery_only_marks_acknowledged_events(self):
        screen, entry = fixture()
        base = entry["data"]["items"]["PFE"]
        screen["candidates"] = [{"symbol": f"T{i}", "name": "x" * 100} for i in range(32)]
        entry["data"]["items"] = {f"T{i}": {**base, "symbol": f"T{i}"} for i in range(32)}
        events = alerts.eligible_movements(screen, entry, now=NOW)
        batches = alerts._batch_events(events)
        self.assertGreater(len(batches), 1)
        with mock.patch.object(alerts, "send_message",
                               side_effect=[10] + [alerts.TelegramError("safe", status=503)] * 10) as send:
            result = self.notifier.process(screen, entry, now=NOW)
            self.assertEqual(result["sent"], len(batches[0]))
            self.assertEqual(send.call_count, 2)
            sent = json.loads(Path(self.state_path).read_text())["destinations"]
            self.assertEqual(len(sent[self.notifier._destination_key()]["sent"]), len(batches[0]))

    def test_disabled_demo_and_missing_credentials_never_call_transport(self):
        with mock.patch.object(alerts, "_api_request") as request:
            for key, value in (("TELEGRAM_ENABLED", False), ("DEMO", True),
                               ("TELEGRAM_BOT_TOKEN", ""), ("TELEGRAM_CHAT_ID", "")):
                with mock.patch.object(config, key, value):
                    self.notifier.process(*fixture(), now=NOW)
            request.assert_not_called()
            self.assertFalse(Path(self.state_path).exists())

    def test_unwritable_state_does_not_crash_or_resend_in_same_process(self):
        with mock.patch.object(config, "TELEGRAM_STATE_FILE", str(Path(self.tmp.name) / "missing" / "state.json")), \
             mock.patch.object(alerts.os, "makedirs", side_effect=PermissionError("secret details")), \
             mock.patch.object(alerts, "send_message", return_value=10) as send:
            self.assertEqual(self.notifier.process(*fixture(), now=NOW)["status"], "sent")
            self.assertEqual(self.notifier.process(*fixture(), now=NOW)["status"], "already_sent")
            self.assertEqual(send.call_count, 1)
            self.assertIn("could not be saved", self.notifier.status()["last_error"])

    def test_corrupt_state_is_bounded_and_reported(self):
        Path(self.state_path).write_text("not json")
        with mock.patch.object(alerts, "send_message", return_value=10):
            self.assertEqual(self.notifier.process(*fixture(), now=NOW)["status"], "sent")
        self.assertEqual(json.loads(Path(self.state_path).read_text())["version"], 1)
        self.assertIn("could not be read", self.notifier.status()["last_error"])


class TransportTests(AlertTestCase):
    def test_plain_text_json_post_to_fixed_host_without_parse_mode(self):
        with mock.patch.object(alerts._OPENER, "open",
                               return_value=response({"ok": True, "result": {"message_id": 42}})) as op:
            self.assertEqual(alerts.send_message("hello"), 42)
            request = op.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
            payload = json.loads(request.data)
            self.assertEqual(payload["chat_id"], CHAT)
            self.assertEqual(payload["text"], "hello")
            self.assertNotIn("parse_mode", payload)
            self.assertEqual(op.call_args.kwargs["timeout"], 10)

    def test_http_and_network_errors_never_expose_token_or_response_body(self):
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        errors = [
            urllib.error.HTTPError(url, 401, TOKEN, {}, io.BytesIO(b"secret response")),
            urllib.error.URLError(f"connection failed: {url}"),
            TimeoutError(url),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__), \
                 mock.patch.object(alerts._OPENER, "open", side_effect=error):
                with self.assertRaises(alerts.TelegramError) as caught:
                    alerts.send_message("hello")
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertNotIn("secret response", str(caught.exception))

    def test_429_parses_retry_after_without_leaking_body(self):
        error = urllib.error.HTTPError("https://api.telegram.org", 429, "limit", {}, io.BytesIO(
            json.dumps({"parameters": {"retry_after": 37}, "description": TOKEN}).encode()))
        with mock.patch.object(alerts._OPENER, "open", side_effect=error):
            with self.assertRaises(alerts.TelegramError) as caught:
                alerts.send_message("hello")
        self.assertEqual(caught.exception.retry_after, 37)
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_invalid_acknowledgement_is_not_a_success(self):
        for body in ({"ok": False, "result": {"message_id": 1}},
                     {"ok": True, "result": {}}, {"ok": True, "result": []}):
            with self.subTest(body=body), mock.patch.object(alerts._OPENER, "open", return_value=response(body)):
                with self.assertRaises(alerts.TelegramError):
                    alerts.send_message("hello")

    def test_redirects_are_not_followed_with_bot_token(self):
        handler = alerts._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://evil.example"))

    def test_chat_discovery_returns_ids_not_message_contents(self):
        result = {"ok": True, "result": [{"message": {
            "chat": {"id": -100123, "type": "supergroup", "title": "Alerts"},
            "text": "private message must not be returned",
        }}]}
        with mock.patch.object(alerts._OPENER, "open", return_value=response(result)):
            chats = alerts.discover_chats()
        self.assertEqual(chats, [{"id": "-100123", "type": "supergroup", "title": "Alerts"}])


class AppIntegrationTests(AlertTestCase):
    def setUp(self):
        super().setUp()
        saved_cache = copy.deepcopy(app.CACHE)
        saved_registry = list(app.REGISTRY)
        self.addCleanup(lambda: (app.CACHE.clear(), app.CACHE.update(saved_cache)))
        self.addCleanup(lambda: setattr(app, "REGISTRY", saved_registry))
        app.CACHE.clear()

    def test_only_background_quote_refresh_notifies_and_failure_cannot_break_board(self):
        screen, quotes = fixture()
        app.REGISTRY = [
            ("stock_screen", lambda: screen, 3600),
            ("stock_quotes", lambda: quotes["data"], 300),
        ]
        with mock.patch.object(app, "save_disk_cache"), \
             mock.patch.object(alerts, "process_stock_alerts",
                               side_effect=RuntimeError(TOKEN)) as process:
            app.warm_all(force=True)
            process.assert_called_once()
            self.assertIsNotNone(app.cache_get("stock_quotes"))
            with mock.patch.object(app, "fetch_into_cache") as fetch:
                app.warm_all()
                fetch.assert_not_called()
            self.assertEqual(process.call_count, 1)
        with mock.patch.object(alerts, "send_message") as send:
            app.summary()
            render.build_stocks_page(app.summary()["stock_screen"])
            send.assert_not_called()

    def test_status_is_secret_free_and_badge_shares_configured_threshold(self):
        with mock.patch.object(config, "STOCK_ALERT_THRESHOLD_PCT", 4.0):
            status = alerts.status()
            self.assertTrue(status["enabled"])
            self.assertTrue(status["configured"])
            self.assertEqual(status["threshold_pct"], 4.0)
            self.assertNotIn(TOKEN, json.dumps(status))
            self.assertNotIn(CHAT, json.dumps(status))
            screen, entry = fixture(3.5)
            merged = app._with_stock_quotes(screen, entry, now=NOW)
            page = render.build_stocks_page(merged, generated_at=NOW)
            self.assertNotIn('class="stock-move-alert"', page)
            self.assertIn("Telegram sharp-rise alerts: enabled", page)
            self.assertIn("+4% threshold", page)


class SetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("telegram_setup", ROOT / "tools/telegram_alert.py")
        cls.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cli)

    def test_env_loader_is_not_a_shell_and_environment_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# comment\nECON_TELEGRAM_BOT_TOKEN='123:abc'\n"
                            "ECON_TELEGRAM_CHAT_ID=\"-100123\"\nECON_TEST_VALUE=$(touch SHOULD_NOT_EXIST)\n")
            with mock.patch.dict(os.environ, {"ECON_TELEGRAM_CHAT_ID": "999"}, clear=True):
                self.cli.load_env_file(path)
                self.assertEqual(os.environ["ECON_TELEGRAM_BOT_TOKEN"], "123:abc")
                self.assertEqual(os.environ["ECON_TELEGRAM_CHAT_ID"], "999")
                self.assertEqual(os.environ["ECON_TEST_VALUE"], "$(touch SHOULD_NOT_EXIST)")
            self.assertFalse((Path(tmp) / "SHOULD_NOT_EXIST").exists())

    def test_default_check_is_offline_and_systemd_loads_env_and_persistent_state(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("ECON_TELEGRAM_")}
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/telegram_alert.py"), "--check"],
            env=env, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No network request", result.stdout)
        unit = (ROOT / "systemd/econ-dashboard.service").read_text()
        self.assertIn("EnvironmentFile=-__APP_DIR__/.env", unit)
        self.assertIn("StateDirectory=econ", unit)
        self.assertIn("chmod 600 \"$APP_DIR/.env\"", (ROOT / "install.sh").read_text())
        subprocess.run(["bash", "-n", str(ROOT / "install.sh")], check=True)

    def test_explicit_test_bypasses_enable_flag_without_touching_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "settings.env"
            env_file.write_text("")
            with mock.patch.object(config, "TELEGRAM_ENABLED", False), \
                 mock.patch.object(alerts, "send_message", return_value=42) as send, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                code = self.cli.main(["--test", "--env-file", str(env_file)])
            self.assertEqual(code, 0)
            send.assert_called_once()
            self.assertIn("setup test, not a stock-movement alert", send.call_args.args[0])
            self.assertIn("Automatic alerts remain off", output.getvalue())
            self.assertFalse((Path(tmp) / "telegram-state.json").exists())

    def test_discovery_needs_token_but_not_chat_and_prints_no_message_text(self):
        chats = [{"id": "-100123", "type": "supergroup", "title": "Alerts"}]
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "settings.env"
            env_file.write_text("")
            with mock.patch.object(alerts, "discover_chats", return_value=chats) as discover, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                code = self.cli.main(["--discover-chats", "--env-file", str(env_file)])
            self.assertEqual(code, 0)
            discover.assert_called_once()
            self.assertIn("-100123", output.getvalue())
            self.assertNotIn(TOKEN, output.getvalue())

    def test_cli_transport_error_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "settings.env"
            env_file.write_text("")
            with mock.patch.object(alerts, "send_message", side_effect=RuntimeError(TOKEN)), \
                 contextlib.redirect_stderr(io.StringIO()) as output:
                code = self.cli.main(["--test", "--env-file", str(env_file)])
            self.assertEqual(code, 1)
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertIn("RuntimeError", output.getvalue())

    def test_config_defaults_off_and_reloads_server_env(self):
        code = ("import config; print(config.TELEGRAM_ENABLED, config.STOCK_ALERT_THRESHOLD_PCT, "
                "config.TELEGRAM_MAX_QUOTE_AGE)")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ECON_TELEGRAM_", "ECON_STOCK_ALERT_"))}
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "dashboard",
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), "False 3.0 1800")
        env.update(ECON_TELEGRAM_ENABLED="1", ECON_STOCK_ALERT_THRESHOLD="4.5",
                   ECON_TELEGRAM_MAX_QUOTE_AGE="1")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT / "dashboard",
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), "True 4.5 60")


if __name__ == "__main__":
    unittest.main()
