#!/usr/bin/env python3
"""Telegram setup helper. --check is offline; only explicit actions use network.

Read the same ignored .env as systemd, without executing shell code. Exported
environment variables take precedence. Never pass a bot token on the command
line or paste it into a chat. See README.md for the server setup procedure.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_KEY = re.compile(r"ECON_[A-Z0-9_]+\Z")


def load_env_file(path: Path, *, required=False) -> None:
    """Load plain KEY=value assignments, with optional matching outer quotes.

    No shell evaluation, variable expansion or inline-comment interpretation.
    This matches the simple unquoted .env format documented for this project.
    Errors name the line number, never the value (which might be a secret).
    """
    try:
        lines = path.read_text("utf-8").splitlines()
    except FileNotFoundError:
        if required:
            raise ValueError("The specified env file does not exist.") from None
        return
    except OSError:
        raise ValueError("Could not read the env file; check its permissions.") from None
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not _KEY.fullmatch(key):
            raise ValueError(f"Invalid env assignment on line {number}; use ECON_NAME=value.")
        if len(value) >= 2 and value[0] in ("'", '"'):
            if value[-1] != value[0]:
                raise ValueError(f"Unclosed quote in env file on line {number}.")
            value = value[1:-1]
        if any(ord(c) < 32 for c in value):
            raise ValueError(f"Control character in env file on line {number}.")
        os.environ.setdefault(key, value)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="validate settings without network (default)")
    actions.add_argument("--discover-chats", action="store_true",
                         help="list recent chats after you message the bot; requires only its token")
    actions.add_argument("--test", action="store_true",
                         help="send an explicit test message, bypassing the alert enable flag and dedupe")
    parser.add_argument("--env-file", type=Path,
                        help="env file (default: .env in the repository; optional unless explicitly set)")
    args = parser.parse_args(argv)
    explicit_env = args.env_file is not None
    try:
        load_env_file(args.env_file or ROOT / ".env", required=explicit_env)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    sys.path.insert(0, str(ROOT / "dashboard"))
    import alerts
    import config

    try:
        if args.discover_chats:
            chats = alerts.discover_chats()
            if not chats:
                print("No recent chats found. Send your bot a message (or /start in a group), then retry.")
                print("For groups, disable BotFather privacy mode if the bot cannot see your message.")
                return 1
            print("Recent chats — put the intended ID in ECON_TELEGRAM_CHAT_ID in .env:")
            for chat in chats:
                print(f"  {chat['id']}  {chat['type']}  {chat['title']}")
            return 0
        if args.test:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            alerts.send_message(
                "ECON Telegram connection test\n"
                f"Sent at {stamp}.\n"
                "This is a setup test, not a stock-movement alert or trade signal."
            )
            print("Test message accepted by Telegram. Check the destination chat.")
            if not config.TELEGRAM_ENABLED:
                print("Automatic alerts remain off; set ECON_TELEGRAM_ENABLED=1 and restart the service.")
            return 0
        issue = alerts.credentials_error()
        print(f"Automatic Telegram alerts: {'enabled' if config.TELEGRAM_ENABLED else 'disabled'}")
        print(f"Credentials: {'valid format' if issue is None else issue}")
        print(f"Sharp-rise threshold: +{config.STOCK_ALERT_THRESHOLD_PCT:g}% vs previous close")
        print(f"DEMO suppression: {'on' if config.DEMO else 'off'}")
        print("No network request or test message was sent.")
        if config.TELEGRAM_ENABLED and issue:
            return 1
        if issue:
            print("Set ECON_TELEGRAM_BOT_TOKEN and ECON_TELEGRAM_CHAT_ID in the server's .env.")
        return 0
    except alerts.TelegramError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Unknown exception strings can contain the token-bearing request URL.
        print(f"ERROR: Telegram setup failed ({type(exc).__name__}); check settings/server connectivity.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
