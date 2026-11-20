"""Index drawdowns from the highest daily value in available provider history."""
from datetime import date, timedelta
import math

# Yahoo MSCI ticker is the USD price-return index, not an ETF or total return.
INDICES = (
    ("S&P 500", "^GSPC", "SP500"),
    ("Nasdaq 100", "^NDX", "NASDAQ100"),
    ("MSCI World", "^990100-USD-STRD", None),
    ("Dow Jones Industrial Average", "^DJI", "DJIA"),
)
TTL = 60 * 60


def from_history(name, rows, source, *, today=None):
    """Validate observations and calculate a peak and drawdown from closes only.

    The provider's maximum available history is not guaranteed to reach index
    inception, so the peak must never be called a verified all-time record.
    """
    today = today or date.today()
    clean = {}
    for row in rows:
        try:
            day = date.fromisoformat(str(row[0])[:10])
            value = float(row[1])
            if day <= today and value > 0 and math.isfinite(value):
                clean[day] = value
        except (ValueError, TypeError, IndexError, OverflowError):
            continue
    if not clean:
        raise ValueError("no valid daily values")
    ordered = sorted(clean.items())
    current_day, current = ordered[-1]
    if current_day < today - timedelta(days=10):
        raise ValueError(f"latest daily value is stale ({current_day})")
    peak_day, peak = max(ordered, key=lambda item: item[1])
    return {
        "name": name, "current": current, "current_date": current_day.isoformat(),
        "peak": peak, "peak_date": peak_day.isoformat(),
        "drawdown_pct": (current / peak - 1) * 100,
        "history_start": ordered[0][0].isoformat(), "history_end": current_day.isoformat(),
        "source": source,
    }
