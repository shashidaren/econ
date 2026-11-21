"""Index drawdowns from intraday highs and daily closing highs, with live quote overlay."""
from datetime import date, datetime, timedelta, timezone
import math
from zoneinfo import ZoneInfo

# Yahoo MSCI ticker is the USD price-return index, not an ETF or total return.
INDICES = (
    ("S&P 500", "^GSPC", "SP500"),
    ("Nasdaq 100", "^NDX", "NASDAQ100"),
    ("MSCI World", "^990100-USD-STRD", None),
    ("Dow Jones Industrial Average", "^DJI", "DJIA"),
)
# Gold uses the dashboard's XAU/USD quote mapping (USD per troy ounce).
GOLD_NAME = "Gold"
GOLD_SYMBOL = "xauusd"
GOLD_GRAMS_PER_TROY_OUNCE = 31.1034768
GOLD_HISTORY_DAYS = 365
TTL = 60 * 60
QUOTE_TTL = 5 * 60

_ET_TZ = ZoneInfo("America/New_York")


def _pos_finite(raw) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return val if (val > 0 and math.isfinite(val)) else None


def from_history(name, rows, source, *, today=None):
    """Validate observations and calculate closing-high and intraday-high drawdowns.

    Rows may be `(date, close)` (close-only feeds such as FRED or MSCI EOD) or
    `(date, close, high, ...)` (OHLC feeds such as Sina daily K, Nasdaq, Tencent,
    or Yahoo daily bars). When no valid intraday `high` values are present,
    `intraday_peak` is left `None` rather than conflating closes with intraday
    records.

    The provider's maximum available history is not guaranteed to reach index
    inception, so the peak must never be called a verified all-time record.
    """
    today = today or date.today()
    clean: dict[date, tuple[float, float | None]] = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row[0])[:10])
            close = _pos_finite(row[1])
            if close is None or day > today:
                continue
            high = _pos_finite(row[2]) if len(row) > 2 else None
            if high is not None:
                high = max(high, close)
            clean[day] = (close, high)
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
    if not clean:
        raise ValueError("no valid daily values")

    ordered = sorted(clean.items())
    current_day, (current, _last_high) = ordered[-1]
    if current_day < today - timedelta(days=10):
        raise ValueError(f"latest daily value is stale ({current_day})")

    close_peak_day, (close_peak, _) = max(ordered, key=lambda item: item[1][0])
    close_dd = (current / close_peak - 1.0) * 100.0
    if close_dd > 0:
        close_dd = 0.0

    has_intraday = any(h is not None for _d, (_c, h) in ordered)
    if has_intraday:
        intraday_peak_day, (_c, eff_high) = max(
            ((d, (c, h if h is not None else c)) for d, (c, h) in ordered),
            key=lambda item: item[1][1],
        )
        intraday_peak = eff_high
        intraday_peak_date = intraday_peak_day.isoformat()
        intraday_dd = min(0.0, (current / intraday_peak - 1.0) * 100.0)
    else:
        intraday_peak = None
        intraday_peak_date = None
        intraday_dd = None

    return {
        "name": name,
        "current": current,
        "current_date": current_day.isoformat(),
        "current_is_live_quote": False,
        "daily_close": current,
        "daily_close_date": current_day.isoformat(),
        # Backward-compatible closing-high aliases
        "peak": close_peak,
        "peak_date": close_peak_day.isoformat(),
        "drawdown_pct": close_dd,
        # Explicit closing-high & intraday-high fields
        "close_peak": close_peak,
        "close_peak_date": close_peak_day.isoformat(),
        "close_drawdown_pct": close_dd,
        "above_close_peak_pct": 0.0,
        "intraday_peak": intraday_peak,
        "intraday_peak_date": intraday_peak_date,
        "intraday_drawdown_pct": intraday_dd,
        "intraday_peak_note": None,
        "history_start": ordered[0][0].isoformat(),
        "history_end": current_day.isoformat(),
        "source": source,
        "quote": None,
    }


def _parse_iso_day(raw) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except (TypeError, ValueError):
        return None


def _parse_utc_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def apply_quote(item: dict, quote: dict | None, *, now: datetime | None = None) -> dict:
    """Overlay a fresh live / ~15m-delayed index quote onto a historical drawdown item.

    Updates `current` to the quote's index level when the quote is at least as
    recent as the historical close, updates `intraday_peak` if the session high
    exceeds the historical peak, and updates `close_peak` when a newer settled
    session close exceeds the historical closing peak.
    """
    out = dict(item or {})
    if out.get("current") is None or not isinstance(quote, dict):
        return out

    price = _pos_finite(quote.get("price"))
    if price is None:
        return out

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment_utc = moment.astimezone(timezone.utc)
    today_et = moment_utc.astimezone(_ET_TZ).date()

    q_dt = _parse_utc_dt(quote.get("as_of_utc"))
    age_seconds = None
    if q_dt is not None:
        age_seconds = max(0, int((moment_utc - q_dt).total_seconds()))
        if age_seconds > 7 * 86400:
            return out

    daily_day = _parse_iso_day(out.get("daily_close_date") or out.get("current_date"))
    quote_day = _parse_iso_day(quote.get("trade_date"))
    if quote_day is None and q_dt is not None:
        quote_day = q_dt.astimezone(_ET_TZ).date()
    if quote_day is not None and quote_day > today_et + timedelta(days=1):
        quote_day = None
    if quote_day is not None and daily_day is not None and quote_day < daily_day:
        # Never overwrite a newer daily close with an older quote.
        return out

    close_peak = _pos_finite(out.get("close_peak")) or _pos_finite(out.get("peak"))
    if close_peak is None:
        return out
    close_peak_date = out.get("close_peak_date") or out.get("peak_date")

    market_status = str(quote.get("market_status") or "").lower()
    prev_close = _pos_finite(quote.get("prev_close"))
    day_high = _pos_finite(quote.get("day_high"))
    week52_high = _pos_finite(quote.get("week52_high"))

    # If the regular session has closed and this quote represents a settled close
    # on or after the history end, a new closing record updates `close_peak`.
    if (market_status in ("after_hours", "closed")
            and quote_day is not None
            and (daily_day is None or quote_day >= daily_day)
            and price > close_peak):
        close_peak = price
        close_peak_date = quote_day.isoformat()
    elif (prev_close is not None
          and quote_day is not None
          and daily_day is not None
          and (quote_day - daily_day).days >= 2
          and prev_close > close_peak):
        # Covers a 1-session lag in daily history when today's session is open
        # and yesterday's settled close (`prev_close`) set a new closing high.
        close_peak = prev_close
        prev_day = quote_day - timedelta(days=3 if quote_day.weekday() == 0 else 1)
        close_peak_date = prev_day.isoformat()

    raw_close_dd = (price / close_peak - 1.0) * 100.0
    if raw_close_dd > 0:
        close_dd = 0.0
        above_close_peak = raw_close_dd
    else:
        close_dd = raw_close_dd
        above_close_peak = 0.0

    session_high = max(price, day_high) if day_high is not None else price
    intraday_peak = _pos_finite(out.get("intraday_peak"))
    intraday_peak_date = out.get("intraday_peak_date")
    intraday_note = out.get("intraday_peak_note")

    if intraday_peak is not None:
        intraday_peak = max(intraday_peak, close_peak)
        if session_high > intraday_peak:
            intraday_peak = session_high
            intraday_peak_date = (quote_day.isoformat() if quote_day
                                  else out.get("current_date"))
            intraday_note = None
        if week52_high is not None and week52_high > intraday_peak * 1.0001:
            intraday_peak = week52_high
            intraday_peak_date = None
            intraday_note = "52-week intraday high from quote feed"
        intraday_dd = min(0.0, (price / intraday_peak - 1.0) * 100.0)
    else:
        # Close-only history fallback (e.g., FRED): use 52-week intraday high only
        # when the historical closing peak falls within the last 52 weeks, so a
        # multi-year peak older than 52 weeks is never understated.
        peak_day_obj = _parse_iso_day(close_peak_date)
        within_52w = (
            peak_day_obj is not None
            and (today_et - peak_day_obj).days <= 365
        )
        if week52_high is not None and within_52w:
            cand_high = max(close_peak, session_high, week52_high)
            intraday_peak = cand_high
            intraday_peak_date = (
                (quote_day.isoformat() if quote_day else out.get("current_date"))
                if session_high >= max(week52_high, close_peak)
                else None
            )
            intraday_note = "52-week intraday high (history fallback is close-only)"
            intraday_dd = min(0.0, (price / intraday_peak - 1.0) * 100.0)
        else:
            intraday_dd = None

    q_copy = dict(quote)
    q_copy["age_seconds"] = age_seconds

    out["current"] = price
    out["current_date"] = (quote_day.isoformat() if quote_day
                           else out.get("daily_close_date") or out.get("current_date"))
    out["current_is_live_quote"] = True
    out["peak"] = close_peak
    out["peak_date"] = close_peak_date
    out["drawdown_pct"] = close_dd
    out["close_peak"] = close_peak
    out["close_peak_date"] = close_peak_date
    out["close_drawdown_pct"] = close_dd
    out["above_close_peak_pct"] = above_close_peak
    out["intraday_peak"] = intraday_peak
    out["intraday_peak_date"] = intraday_peak_date
    out["intraday_drawdown_pct"] = intraday_dd
    out["intraday_peak_note"] = intraday_note
    out["quote"] = q_copy
    return out
