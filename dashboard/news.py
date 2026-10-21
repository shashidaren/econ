"""Recent stock headlines from key-less public RSS feeds.

The Stocks tab answers "what moved, and why might it have moved?". This module
supplies the second half: up to a few recent headlines per *displayed* ticker.

Design rules
------------
* **Context only.** Headlines never enter the screen: they cannot add, remove,
  reorder, or rescore a candidate. They are fetched after the ranking is final,
  and a feed outage can never fail the screen.
* **Key-less and cache-friendly.** Three public RSS 2.0 endpoints are tried per
  symbol, in order, using the shared provider breakers in `sources.py`:

      Google News RSS  ->  Bing News RSS  ->  Yahoo Finance headline RSS

  One request per symbol per screen refresh (at most once an hour), so a full
  seven-name list costs at most seven small RSS requests.
* **No invented recency.** An item is kept only when its publication date is
  inside the window (`days`, default 7). Undated items are dropped rather than
  presented as "recent".
* **No invented relevance.** A headline must mention the ticker (as a word) or
  a distinctive part of the company name. The check is deliberately loose
  because failures are visible (the item is dropped, not replaced), but it
  stops an unrelated result from a fallback feed from appearing under a ticker.
* **Failures stay visible.** Per-symbol errors are returned with the data, and
  `--doctor` probes each feed so the LXC owner can see which one answered.

Nothing here is a recommendation, and no headline is verified for accuracy.
"""

from __future__ import annotations

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import sources

DEFAULT_WINDOW_DAYS = 7
MAX_ITEMS_PER_SYMBOL = 3
MAX_SYMBOLS = 32          # matches the screen's hard display ceiling
FETCH_TIMEOUT = 8
CONNECT_TIMEOUT = 4

# (breaker name, human label) — order here is the fallback order.
PROVIDERS = (
    ("google_news", "Google News RSS"),
    ("bing_news", "Bing News RSS"),
    ("yahoo_news", "Yahoo Finance RSS"),
)

# Corporate/legal filler that must not be enough to call a headline relevant.
_STOPWORDS = {
    "inc", "corp", "corporation", "company", "co", "group", "holdings",
    "holding", "brands", "technologies", "technology", "enterprises",
    "enterprise", "solutions", "industries", "plc", "adr", "sa", "the",
    "and", "of", "for",
}

_LIMITATIONS = (
    "Third-party RSS headlines, newest first, kept only when the publication "
    "date is inside the window and the title mentions the ticker or company. "
    "They are not verified, may be syndicated or duplicated elsewhere, and are "
    "never used by the screen or its ranking. Some publishers block feed "
    "readers, so a ticker can legitimately have no headlines."
)


# ---------------------------------------------------------------------------
# Feed URLs
# ---------------------------------------------------------------------------

def google_news_url(name: str, symbol: str, days: int = DEFAULT_WINDOW_DAYS) -> str:
    query = f'"{name}" stock when:{days}d'
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
            + "&hl=en-US&gl=US&ceid=US:en")


def bing_news_url(name: str, symbol: str, days: int = DEFAULT_WINDOW_DAYS) -> str:
    query = f'"{name}" stock'
    return ("https://www.bing.com/news/search?q=" + urllib.parse.quote(query)
            + "&format=RSS&setlang=en-US&cc=US")


def yahoo_news_url(name: str, symbol: str, days: int = DEFAULT_WINDOW_DAYS) -> str:
    return ("https://feeds.finance.yahoo.com/rss/2.0/headline?s="
            + urllib.parse.quote(symbol) + "&region=US&lang=en-US")


_URL_BUILDERS = (
    ("google_news", "Google News RSS", google_news_url),
    ("bing_news", "Bing News RSS", bing_news_url),
    ("yahoo_news", "Yahoo Finance RSS", yahoo_news_url),
)


# ---------------------------------------------------------------------------
# Feed parsing (RSS 2.0 + Atom, namespace-tolerant)
# ---------------------------------------------------------------------------

def _local(tag) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _text(node) -> str:
    return (node.text or "").strip()


def _parse_date(raw):
    """Parse RFC-822 (RSS) or ISO-8601 (Atom) into an aware UTC datetime."""
    raw = (raw or "").strip()
    if not raw:
        return None
    moment = None
    try:
        moment = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        moment = None
    if moment is None:
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if not isinstance(moment, datetime):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def parse_feed(text: str) -> list[dict]:
    """Return [{title, link, published, publisher}] from an RSS/Atom body.

    Never raises: a malformed or empty body yields an empty list, which the
    caller treats as a provider failure.
    """
    if not text or "<" not in text:
        return []
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError:
        return []
    out = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields = {}
        links = []
        for child in list(node):
            name = _local(child.tag)
            if name == "link":
                href = (child.get("href") or "").strip()
                value = href or _text(child)
                if value:
                    links.append(value)
            elif name in ("title", "pubdate", "published", "updated",
                          "description", "summary", "guid", "id", "source",
                          "creator", "author"):
                value = _text(child)
                if value and name not in fields:
                    fields[name] = value
        title = html.unescape(re.sub(r"\s+", " ", fields.get("title", ""))).strip()
        if not title:
            continue
        publisher = html.unescape(
            fields.get("source") or fields.get("creator") or ""
        ).strip()
        published = _parse_date(fields.get("pubdate") or fields.get("published")
                                or fields.get("updated"))
        entry = {
            "title": title,
            "link": next((link for link in links
                          if link.startswith(("http://", "https://"))), None),
            "published": published,
            "publisher": publisher or _publisher_from_link(
                next((link for link in links if link), None)),
        }
        out.append(entry)
    return out


_HOST_LABELS = {
    "bing.com": "Bing News",
    "news.google.com": "Google News",
    "finance.yahoo.com": "Yahoo Finance",
    "yahoo.com": "Yahoo Finance",
}


def _publisher_from_link(link):
    if not link:
        return None
    host = urllib.parse.urlparse(link).netloc.lower()
    if not host:
        return None
    host = host[4:] if host.startswith("www.") else host
    return _HOST_LABELS.get(host, host or None)


# ---------------------------------------------------------------------------
# Cleaning, relevance and selection
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    text = re.sub(r"[^a-z0-9&]+", " ", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def clean_title(title: str, publisher: str | None = None) -> str:
    """Tidy a headline and remove Google's trailing ' - Publisher' suffix."""
    title = re.sub(r"\s+", " ", (title or "")).strip()
    if publisher:
        suffix = " - " + publisher.strip().lower()
        if title.lower().endswith(suffix):
            title = title[: -(len(publisher.strip()) + 3)].strip()
    return title


def is_relevant(title: str, symbol: str, name: str) -> bool:
    """True when the headline plausibly belongs to this ticker.

    Deliberately permissive (the feed query already carries most of the
    filtering) but strict enough to reject another company's headline from a
    broad fallback feed. Single-letter tickers are not matched as words: "T"
    and "C" produce too much noise to be evidence of anything.
    """
    normalized = " " + _normalize(title) + " "
    sym = (symbol or "").strip().lower()
    if len(sym) >= 2 and f" {sym} " in normalized:
        return True
    norm_name = _normalize(name)
    if norm_name and norm_name in normalized:
        return True
    compact_name = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    compact_title = re.sub(r"[^a-z0-9]", "", (title or "").lower())
    if len(compact_name) >= 4 and compact_name in compact_title:
        return True
    tokens = [t for t in norm_name.replace("&", " ").split()
              if len(t) >= 3 and t not in _STOPWORDS]
    return any(f" {token} " in normalized for token in tokens)


def select_items(raw_items, symbol: str, name: str, *, days: int,
                 per_symbol: int, now: datetime) -> tuple[list[dict], dict]:
    """Filter parsed feed items to recent, relevant, de-duplicated headlines."""
    since = now - timedelta(days=days)
    future_slack = now + timedelta(days=1)      # tolerate publisher clock skew
    dropped = {"undated": 0, "outside_window": 0, "off_topic": 0,
               "duplicate": 0, "too_short": 0}
    seen = set()
    kept = []
    for item in raw_items or []:
        title = clean_title(item.get("title"), item.get("publisher"))
        if len(title) < 12:
            dropped["too_short"] += 1
            continue
        published = item.get("published")
        if not isinstance(published, datetime):
            dropped["undated"] += 1
            continue
        if published < since or published > future_slack:
            dropped["outside_window"] += 1
            continue
        if not is_relevant(title, symbol, name):
            dropped["off_topic"] += 1
            continue
        key = _normalize(title)
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        link = item.get("link")
        kept.append({
            "title": title,
            "link": link if isinstance(link, str) and link.startswith(
                ("http://", "https://")) else None,
            "publisher": (item.get("publisher") or "unknown publisher")[:80],
            "published_utc": published.isoformat(timespec="minutes"),
            "_published": published,
        })
    kept.sort(key=lambda entry: entry["_published"], reverse=True)
    kept = kept[:max(1, per_symbol)]
    for entry in kept:
        entry.pop("_published", None)
    return kept, dropped


# ---------------------------------------------------------------------------
# Per-symbol cascade
# ---------------------------------------------------------------------------

def fetch_symbol(candidate, *, days: int, per_symbol: int, now: datetime,
                 getter) -> dict:
    """Try each headline feed for one ticker; return items or visible errors."""
    symbol = str(candidate.get("symbol") or "").strip().upper()
    name = str(candidate.get("name") or symbol).strip() or symbol
    errors = []
    for provider, label, builder in _URL_BUILDERS:
        if not sources.provider_available(provider):
            errors.append(f"{label}: cooling down after repeated failures")
            continue
        try:
            body = getter(builder(name, symbol, days), timeout=FETCH_TIMEOUT,
                          connect_timeout=CONNECT_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — one feed must not stop the rest
            unreachable, limited = sources._classify(exc)
            sources.note_provider_failure(provider, exc,
                                          unreachable=unreachable,
                                          rate_limited=limited)
            errors.append(f"{label}: {str(exc)[:140]}")
            continue
        raw = parse_feed(body)
        if not raw:
            sources.note_provider_failure(provider, "empty or unparseable feed")
            errors.append(f"{label}: empty or unparseable feed")
            continue
        sources.note_provider_ok(provider)
        items, dropped = select_items(raw, symbol, name, days=days,
                                      per_symbol=per_symbol, now=now)
        if items:
            return {"symbol": symbol, "name": name, "items": items,
                    "provider": label, "errors": errors or None,
                    "dropped": dropped}
        errors.append(f"{label}: {len(raw)} item(s), none inside the "
                      f"{days}-day window and matching this ticker")
    return {"symbol": symbol, "name": name, "items": [], "provider": None,
            "errors": errors, "dropped": None}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fetch_news(candidates, *, days: int = DEFAULT_WINDOW_DAYS,
               per_symbol: int = MAX_ITEMS_PER_SYMBOL, now: datetime | None = None,
               getter=None) -> dict:
    """Fetch recent headlines for the displayed candidates.

    Returns a dict that is always safe to serialise and render:

        status      ok | partial | empty | unavailable
        message     one human sentence for the page
        items       {SYMBOL: [{title, link, publisher, published_utc}]}
        errors      {SYMBOL: "provider: reason; ..."}
        coverage    {symbols, with_headlines, headlines, providers}
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    getter = getter or sources._get
    try:
        days = max(1, min(int(days), 30))
    except (TypeError, ValueError):
        days = DEFAULT_WINDOW_DAYS
    try:
        per_symbol = max(1, min(int(per_symbol), 5))
    except (TypeError, ValueError):
        per_symbol = MAX_ITEMS_PER_SYMBOL

    wanted, seen = [], set()
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        symbol = str(candidate.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        wanted.append(candidate)
        if len(wanted) >= MAX_SYMBOLS:
            break

    base = {
        "window_days": days,
        "per_symbol_limit": per_symbol,
        "fetched_at_utc": now.isoformat(timespec="minutes"),
        "limitations": _LIMITATIONS,
    }
    if not wanted:
        return {**base, "status": "unavailable",
                "message": "No stock candidates to look up headlines for.",
                "items": {}, "errors": {},
                "coverage": {"symbols": 0, "with_headlines": 0,
                             "headlines": 0, "providers": []}}

    workers = max(1, min(4, len(wanted)))
    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="stock-news") as pool:
        results = list(pool.map(
            lambda candidate: fetch_symbol(candidate, days=days,
                                           per_symbol=per_symbol, now=now,
                                           getter=getter),
            wanted,
        ))

    items, errors, providers = {}, {}, []
    for result in results:
        symbol = result["symbol"]
        if result["items"]:
            items[symbol] = result["items"]
            if result["provider"] and result["provider"] not in providers:
                providers.append(result["provider"])
        # Keep fallback detail even when a later provider answered: it explains
        # why a symbol used Bing or Yahoo instead of the primary feed.
        if result.get("errors"):
            errors[symbol] = "; ".join(result["errors"])
    headline_count = sum(len(entries) for entries in items.values())
    coverage = {
        "symbols": len(wanted),
        "with_headlines": len(items),
        "headlines": headline_count,
        "providers": providers,
    }

    if items and len(items) == len(wanted):
        status = "ok"
        message = (f"Recent headlines found for all {len(wanted)} displayed "
                   f"stock(s) ({headline_count} headline(s) inside {days} days).")
    elif items:
        status = "partial"
        missing = len(wanted) - len(items)
        message = (f"Recent headlines found for {len(items)} of {len(wanted)} "
                   f"displayed stock(s); {missing} had no in-window item or a "
                   f"feed error.")
    else:
        status = "unavailable"
        first_error = next(iter(errors.values()), "")
        message = ("Headline feeds returned nothing usable"
                   + (f": {first_error.split(';')[0]}" if first_error else "."))
    return {**base, "status": status, "message": message, "items": items,
            "errors": errors, "coverage": coverage}
