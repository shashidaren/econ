"""Opt-in Telegram delivery for the Stocks tab's sharp-rise alert.

Only freshly fetched, timestamped quotes for displayed candidates are eligible.
Delivery is plain text, batched within Telegram's size limit, and never changes
the screen. Successful deliveries are recorded per destination, ticker,
and U.S. trading session, including across service restarts.

This is best-effort notification, not an execution or guaranteed-delivery system:
a crash between Telegram accepting a message and the state-file write can cause
a duplicate. Never log the bot token, chat ID, or Telegram's raw error body.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

_ET = ZoneInfo("America/New_York")
_MYT = timezone(timedelta(hours=8), "MYT")
_TOKEN_RE = re.compile(r"[0-9]+:[A-Za-z0-9_-]+")
_CHAT_RE = re.compile(r"(?:-?[0-9]+|@[A-Za-z][A-Za-z0-9_]{4,31})")
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.-]{0,14}")
_STATE_VERSION = 1
_MAX_MESSAGE = 3800  # leave headroom below Telegram's 4096-character limit


class TelegramError(RuntimeError):
    """A credential-free error, safe to show in logs and /api/status."""

    def __init__(self, message, *, status=None, retry_after=60):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward a URL containing the bot token to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def credentials_error() -> str | None:
    if not _TOKEN_RE.fullmatch(config.TELEGRAM_BOT_TOKEN):
        return "Telegram bot token is missing or invalid."
    if not _CHAT_RE.fullmatch(config.TELEGRAM_CHAT_ID):
        return "Telegram chat ID is missing or invalid."
    return None


def _utc(value=None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, datetime):
        raise ValueError("expected an epoch or datetime")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_utc(raw) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _number(raw) -> float | None:
    try:
        value = float(raw)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _plain(raw, limit=100) -> str:
    # Telegram receives plain text (no Markdown/HTML parse mode). Keep untrusted
    # provider/company strings to one bounded line, without control characters.
    return "".join(c if c.isprintable() else " " for c in str(raw or ""))[:limit].strip()


def eligible_movements(screen, quotes_entry, *, now=None) -> list[dict]:
    """Return the same >= threshold rises as the badge, with strict freshness.

    Cached/outage/demo payloads, undated or old prints, and non-regular sessions
    are deliberately excluded. The regular-session change is used, never an
    extended-hours percentage. Only the displayed screen candidates are read.
    """
    moment = _utc(now)
    if (config.DEMO or not config.STOCK_QUOTES_ENABLED
            or not isinstance(screen, dict) or screen.get("demo")
            or not isinstance(quotes_entry, dict) or quotes_entry.get("err")):
        return []
    quotes = quotes_entry.get("data")
    if not isinstance(quotes, dict) or quotes.get("demo"):
        return []
    if quotes.get("status") not in ("ok", "partial") or quotes.get("served_from_cache"):
        return []
    session = quotes.get("market_session")
    if not isinstance(session, dict) or session.get("state") != "regular":
        return []

    # Both the cache write and provider's fetch timestamp must be recent. A
    # restored disk cache is not evidence of a new movement.
    max_batch_age = max(600, 2 * config.TTL.get("stock_quotes", 300))
    try:
        batch_age = (moment - _utc(quotes_entry.get("at"))).total_seconds()
    except (TypeError, ValueError, OverflowError, OSError):
        return []
    fetched_at = _parse_utc(quotes.get("fetched_at_utc"))
    if fetched_at is None:
        return []
    fetch_age = (moment - fetched_at).total_seconds()
    if not (-120 <= batch_age <= max_batch_age and -120 <= fetch_age <= max_batch_age):
        return []

    items = quotes.get("items")
    if not isinstance(items, dict):
        return []
    found = []
    seen = set()
    for candidate in screen.get("candidates") or []:
        if not isinstance(candidate, dict):
            continue
        symbol = str(candidate.get("symbol") or "").strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol) or symbol in seen:
            continue
        seen.add(symbol)
        quote = items.get(symbol)
        if not isinstance(quote, dict) or quote.get("demo"):
            continue
        price = _number(quote.get("price"))
        change_pct = _number(quote.get("change_pct"))
        quoted_at = _parse_utc(quote.get("as_of_utc"))
        if price is None or price <= 0 or change_pct is None or quoted_at is None:
            continue
        age = (moment - quoted_at).total_seconds()
        if not -120 <= age <= config.TELEGRAM_MAX_QUOTE_AGE:
            continue
        quoted_et = quoted_at.astimezone(_ET)
        if quoted_et.date() != moment.astimezone(_ET).date():
            continue
        if change_pct < config.STOCK_ALERT_THRESHOLD_PCT:
            continue
        found.append({
            "symbol": symbol,
            "name": _plain(candidate.get("name") or symbol),
            "price": price,
            "prev_close": _number(quote.get("prev_close")),
            "change_pct": change_pct,
            "session": quoted_et.date().isoformat(),
            "as_of_utc": quoted_at.isoformat(timespec="seconds"),
            "source": _plain(quote.get("source") or "delayed quote", 60),
        })
    return found


def _movement_block(event) -> str:
    stamp = _parse_utc(event["as_of_utc"]).astimezone(_MYT)
    previous = event.get("prev_close")
    previous_text = f" (previous close ${previous:,.2f})" if previous and previous > 0 else ""
    return (
        f"{event['symbol']} — {event['name']}\n"
        f"{event['change_pct']:+.2f}% vs previous close\n"
        f"Delayed price: ${event['price']:,.2f}{previous_text}\n"
        f"Quote: {stamp:%Y-%m-%d %H:%M} MYT · {event['source']}"
    )


def _message_length(text: str) -> int:
    # Telegram's character limit counts UTF-16 code units, not Python code points.
    return len(text.encode("utf-16-le")) // 2


def _batch_events(events) -> list[list[dict]]:
    batches, group = [], []
    for event in events:
        size = _message_length(_movement_block(event))
        if group and sum(_message_length(_movement_block(e)) + 2 for e in group) + size > _MAX_MESSAGE - 300:
            batches.append(group)
            group = []
        group.append(event)
    if group:
        batches.append(group)
    return batches


def _format_batch(group, threshold) -> str:
    interval = max(30, config.REFRESH_SECONDS, config.TTL.get("stock_quotes", 300))
    return (
        f"ECON stock movement · {len(group)} sharp rise{'s' if len(group) != 1 else ''}\n\n"
        + "\n\n".join(_movement_block(event) for event in group)
        + f"\n\nThreshold: +{threshold:g}% vs previous close."
        + f"\nPublic quotes can be ~15 minutes delayed; checked about every {interval / 60:g} minutes."
        + "\nOne alert per ticker per U.S. session. Observed move, not a buy signal."
    )


def format_messages(events, threshold=None) -> list[str]:
    """Batch a refresh into plain-text messages, respecting Telegram's limit."""
    threshold = config.STOCK_ALERT_THRESHOLD_PCT if threshold is None else threshold
    return [_format_batch(group, threshold) for group in _batch_events(events)]


def _api_request(method: str, payload: dict, *, token=None) -> dict:
    """POST to the fixed Telegram host with bounded, credential-safe errors.

    No automatic POST retry: an ambiguous timeout may already have delivered.
    Redirects are disabled so a redirect cannot receive the token-bearing URL.
    """
    token = config.TELEGRAM_BOT_TOKEN if token is None else token
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise TelegramError("Telegram bot token is missing or invalid.", retry_after=900)
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=10) as response:
            body = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        retry_after = 60
        if exc.code == 429:
            try:
                error_body = json.loads(exc.read(65536))
                delay = float((error_body.get("parameters") or {}).get("retry_after", 300))
                if math.isfinite(delay) and delay > 0:
                    retry_after = math.ceil(delay)
            except (TypeError, ValueError, AttributeError, OSError, http.client.HTTPException):
                retry_after = 300
        exc.close()
        note = {
            400: "request rejected; check the chat ID and bot access",
            401: "unauthorized; check the bot token",
            403: "bot blocked or lacking permission to message this chat",
            404: "bot API endpoint not found; check the bot token",
            429: "rate limited",
        }.get(exc.code, "service unavailable" if exc.code >= 500 else "request rejected")
        raise TelegramError(f"Telegram HTTP {exc.code}: {note}.",
                            status=exc.code, retry_after=retry_after) from None
    except urllib.error.URLError as exc:
        # URLError's string can contain the full request URL, including token.
        reason = type(exc.reason).__name__
        raise TelegramError(f"Telegram network failure ({reason}).", retry_after=60) from None
    except (TimeoutError, OSError, http.client.HTTPException):
        raise TelegramError("Telegram network failure or timeout.", retry_after=60) from None
    try:
        result = json.loads(body)
        if result.get("ok") is not True or "result" not in result:
            raise ValueError("not an accepted request")
        return result
    except (TypeError, ValueError, KeyError, AttributeError):
        raise TelegramError("Telegram returned an invalid API acknowledgement.",
                            retry_after=60) from None


def send_message(text: str, *, token=None, chat_id=None) -> int:
    """Send plain text and return the ID only after Telegram acknowledges it."""
    chat_id = config.TELEGRAM_CHAT_ID if chat_id is None else chat_id
    if not isinstance(chat_id, str) or not _CHAT_RE.fullmatch(chat_id):
        raise TelegramError("Telegram chat ID is missing or invalid.", retry_after=900)
    if not text or _message_length(text) > 4096:
        raise TelegramError("Telegram message must contain 1–4096 characters.", retry_after=900)
    result = _api_request("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "link_preview_options": {"is_disabled": True},
    }, token=token)
    message_id = result["result"].get("message_id") if isinstance(result["result"], dict) else None
    if not isinstance(message_id, int):
        raise TelegramError("Telegram returned no delivery acknowledgement.", retry_after=60)
    return message_id


def discover_chats(*, token=None) -> list[dict]:
    """Read recent bot chats after the owner messages the bot; no message text.

    getUpdates without an offset does not consume updates. This intentionally
    fails on bots using webhooks rather than changing their webhook settings.
    """
    result = _api_request("getUpdates", {"timeout": 0, "limit": 100}, token=token)
    if not isinstance(result["result"], list):
        raise TelegramError("Telegram returned an invalid chat list.")
    chats = {}
    for update in result["result"]:
        if not isinstance(update, dict):
            continue
        for kind in ("message", "channel_post", "my_chat_member"):
            item = update.get(kind)
            chat = item.get("chat") if isinstance(item, dict) else None
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                chats[str(chat["id"])] = {
                    "id": str(chat["id"]),
                    "type": _plain(chat.get("type"), 30),
                    "title": _plain(chat.get("title") or chat.get("username") or "private chat"),
                }
    return list(chats.values())


class StockAlertNotifier:
    """Single-refresher notifier with persistent dedupe and failure cooldown."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fingerprint = None
        self._state = {"version": _STATE_VERSION, "destinations": {}}
        self._state_error = None
        self._load_warning = None
        self.last_attempt_utc = None
        self.last_success_utc = None
        self.last_error = None

    def _destination_key(self) -> str:
        # Neither the token nor the chat ID is written to disk or exposed in API.
        # Rotating this bot's token must not replay alerts to the same chat.
        material = f"telegram\0{config.TELEGRAM_CHAT_ID}"
        return hashlib.sha256(material.encode()).hexdigest()

    def _load(self, key):
        location = (key, config.TELEGRAM_STATE_FILE)
        if self._fingerprint == location:
            return
        self._fingerprint = location
        self._state = {"version": _STATE_VERSION, "destinations": {}}
        self._state_error = None
        self._load_warning = None
        try:
            with open(config.TELEGRAM_STATE_FILE, encoding="utf-8") as handle:
                raw = json.load(handle)
            if (isinstance(raw, dict) and raw.get("version") == _STATE_VERSION
                    and isinstance(raw.get("destinations"), dict)):
                self._state = raw
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self._load_warning = "Notification state could not be read; duplicates are possible after a restart."
        # Keep a bounded history so switching chats and switching back does not
        # replay the current session. Only this small local service writes state.
        destinations = {}
        for saved_key, saved in list(self._state["destinations"].items())[-7:]:
            if (isinstance(saved_key, str) and re.fullmatch(r"[0-9a-f]{64}", saved_key)
                    and isinstance(saved, dict) and isinstance(saved.get("sent"), dict)):
                destinations[saved_key] = saved
        destination = destinations.get(key)
        if not isinstance(destination, dict) or not isinstance(destination.get("sent"), dict):
            destination = {"sent": {}, "retry_not_before": 0}
        destination["sent"] = {
            k: v for k, v in destination["sent"].items()
            if isinstance(k, str) and isinstance(v, dict)
        }
        destinations.pop(key, None)
        destinations[key] = destination
        self._state["destinations"] = destinations

    def _save(self):
        path = config.TELEGRAM_STATE_FILE
        tmp = None
        try:
            parent = os.path.dirname(path) or "."
            os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".econ-telegram-", dir=parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)  # atomic; mkstemp creates the file mode 0600
            self._state_error = None
        except (OSError, TypeError, ValueError):
            self._state_error = "Notification state could not be saved; duplicates are possible after a restart."
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def process(self, screen, quotes_entry, *, now=None) -> dict:
        if not config.TELEGRAM_ENABLED:
            return {"status": "disabled", "sent": 0}
        if config.DEMO:
            return {"status": "demo_suppressed", "sent": 0}
        error = credentials_error()
        if error:
            return {"status": "misconfigured", "sent": 0, "error": error}
        events = eligible_movements(screen, quotes_entry, now=now)
        if not events:
            return {"status": "no_new_eligible_quotes", "sent": 0}
        moment = _utc(now)
        with self._lock:
            key = self._destination_key()
            self._load(key)
            destination = self._state["destinations"][key]
            try:
                cooldown = float(destination.get("retry_not_before", 0))
            except (TypeError, ValueError):
                cooldown = 0
            if math.isfinite(cooldown) and moment.timestamp() < cooldown:
                return {"status": "cooldown", "sent": 0}
            pending = [e for e in events
                       if f"{e['session']}:{e['symbol']}:rise" not in destination["sent"]]
            if not pending:
                return {"status": "already_sent", "sent": 0}
            sent = 0
            for batch in _batch_events(pending):
                text = _format_batch(batch, config.STOCK_ALERT_THRESHOLD_PCT)
                self.last_attempt_utc = moment.isoformat(timespec="seconds")
                try:
                    send_message(text)
                except TelegramError as exc:
                    self.last_error = str(exc)
                    delay = exc.retry_after if exc.status == 429 else (
                        60 if exc.status is None or exc.status >= 500 else 900
                    )
                    destination["retry_not_before"] = moment.timestamp() + delay
                    self._save()
                    return {"status": "failed", "sent": sent, "error": str(exc)}
                # Only acknowledged events count as delivered. Mark each batch
                # separately so a later failed batch cannot replay an earlier one.
                for event in batch:
                    destination["sent"][f"{event['session']}:{event['symbol']}:rise"] = {
                        "at": moment.isoformat(timespec="seconds"),
                        "change_pct": event["change_pct"],
                    }
                sent += len(batch)
                destination["retry_not_before"] = 0
                cutoff = (moment.astimezone(_ET).date() - timedelta(days=14)).isoformat()
                destination["sent"] = {
                    k: v for k, v in destination["sent"].items() if k[:10] >= cutoff
                }
                self.last_success_utc = moment.isoformat(timespec="seconds")
                self.last_error = None
                self._save()
            return {"status": "sent", "sent": sent}

    def status(self) -> dict:
        """Safe configuration/delivery diagnostics; never include credentials."""
        with self._lock:
            return {
                "enabled": bool(config.TELEGRAM_ENABLED),
                "configured": credentials_error() is None,
                "demo_suppressed": bool(config.DEMO),
                "threshold_pct": config.STOCK_ALERT_THRESHOLD_PCT,
                "direction": "rise",
                "scope": "displayed stock candidates, regular U.S. session",
                "dedupe": "once per ticker per U.S. trading session",
                "max_quote_age_seconds": config.TELEGRAM_MAX_QUOTE_AGE,
                "last_attempt_utc": self.last_attempt_utc,
                "last_success_utc": self.last_success_utc,
                "last_error": self.last_error or self._state_error or self._load_warning,
            }


_NOTIFIER = StockAlertNotifier()


def process_stock_alerts(screen, quotes_entry, *, now=None) -> dict:
    result = _NOTIFIER.process(screen, quotes_entry, now=now)
    if result.get("error") and result["status"] == "failed":
        print(f"[telegram] delivery deferred: {result['error']}", flush=True)
    issue = _NOTIFIER._state_error or _NOTIFIER._load_warning
    if issue:
        print(f"[telegram] {issue}", flush=True)
    return result


def status() -> dict:
    return _NOTIFIER.status()
