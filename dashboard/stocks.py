"""Rules-based short-term stock watchlist screen.

This module deliberately does not forecast returns or make buy recommendations.
It ranks a small, editable US ticker universe using recent daily price/volume
history, then labels observed volatility/drawdown risk. No fundamentals or news
are available in the free, keyless data pipeline, so the output is a watchlist
for further research only.
"""

from datetime import date, datetime, timezone
import math
import statistics


MAX_RESULTS = 7                  # default: show every passer, up to seven
MAX_RESULTS_CEILING = 32         # hard safety cap (the configured universe size)
MIN_HISTORY = 61                 # enough for 60-session momentum/drawdown
MAX_QUOTE_AGE_DAYS = 5           # weekends/holidays included


def _spec_parts(spec):
    """Accept config tuples or dict specs; keep this module easy to test."""
    if isinstance(spec, dict):
        return (
            str(spec.get("symbol") or "").upper(),
            str(spec.get("name") or spec.get("symbol") or ""),
            str(spec.get("sector") or "Other"),
        )
    symbol, name, sector = spec[:3]
    return str(symbol).upper(), str(name), str(sector)


def _rows_for(item):
    """Normalize source rows to [(ISO date, close, volume-or-None)]."""
    if isinstance(item, dict):
        rows = item.get("rows") or item.get("history") or []
        source = item.get("source") or "unknown"
    else:
        rows, source = item or [], "unknown"
    out = {}
    for row in rows:
        try:
            if isinstance(row, dict):
                day = str(row.get("date") or row.get("day") or "")[:10]
                close = float(row.get("close"))
                raw_volume = row.get("volume")
            else:
                day, close = str(row[0])[:10], float(row[1])
                raw_volume = row[2] if len(row) > 2 else None
            if len(day) != 10 or close <= 0 or not math.isfinite(close):
                continue
            try:
                volume = float(raw_volume) if raw_volume is not None else None
                if volume is not None and (volume < 0 or not math.isfinite(volume)):
                    volume = None
            except (TypeError, ValueError):
                volume = None
            out[day] = (day, close, volume)
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out.values()), source


def _change(now, before):
    return ((now / before) - 1.0) * 100.0 if before else None


def _risk_label(volatility_pct, drawdown_pct):
    """Observed-risk buckets; not a probability of loss or a forecast."""
    if volatility_pct <= 25.0 and drawdown_pct >= -10.0:
        return "Low"
    if volatility_pct <= 50.0 and drawdown_pct >= -20.0:
        return "Medium"
    return "High"


def _metrics(rows):
    closes = [r[1] for r in rows]
    recent = rows[-60:]
    daily_returns = [b / a - 1.0 for a, b in zip(closes[-21:-1], closes[-20:]) if a]
    if len(closes) < MIN_HISTORY or len(daily_returns) < 15:
        return None

    close = closes[-1]
    sma20 = statistics.fmean(closes[-20:])
    sma50 = statistics.fmean(closes[-50:])
    ret5 = _change(close, closes[-6])
    ret20 = _change(close, closes[-21])
    ret60 = _change(close, closes[-61])
    vol20 = statistics.stdev(daily_returns) * math.sqrt(252.0) * 100.0

    peak = 0.0
    drawdown = 0.0
    for value in (r[1] for r in recent):
        peak = max(peak, value)
        if peak:
            drawdown = min(drawdown, (value / peak - 1.0) * 100.0)

    dollar_volumes = [r[1] * r[2] for r in rows[-20:] if r[2] is not None]
    avg_dollar_volume = statistics.fmean(dollar_volumes) if len(dollar_volumes) >= 15 else None
    return {
        "close": close,
        "as_of": rows[-1][0],
        "ret1_pct": _change(close, closes[-2]),
        "ret5_pct": ret5,
        "ret20_pct": ret20,
        "ret60_pct": ret60,
        "sma20": sma20,
        "sma50": sma50,
        "vol20_annual_pct": vol20,
        "drawdown60_pct": drawdown,
        "avg_dollar_volume20": avg_dollar_volume,
    }


def build_screen(
    universe,
    market_data,
    *,
    max_price=50.0,
    min_price=5.0,
    min_avg_dollar_volume=5_000_000.0,
    limit=MAX_RESULTS,
    as_of=None,
    generated_at=None,
    fetch_error=None,
):
    """Build every ranked candidate that passes the screen, up to `limit`.

    Eligibility requires: a fresh quote, $5 <= price < $50, at least $5m average
    daily dollar volume, positive 20-session momentum, and price above its
    20-session average. Ranking combines 5/20/60-session momentum with modest
    realized-volatility/drawdown penalties. All thresholds are intentionally
    visible on the page; this is a technical screen, not a value/fundamental
    screen or a prediction.

    `limit` defaults to MAX_RESULTS (7) and is clamped to MAX_RESULTS_CEILING so
    a misconfigured override cannot dump the whole universe onto the tab. The
    full eligible count is always reported even when the display is capped.
    """
    universe = list(universe or [])
    market_data = market_data or {}
    try:
        requested_limit = int(limit)
    except (TypeError, ValueError):
        requested_limit = MAX_RESULTS
    limit = max(0, min(requested_limit, MAX_RESULTS_CEILING))
    as_of = as_of or date.today()
    if isinstance(as_of, str):
        as_of = date.fromisoformat(as_of[:10])
    if isinstance(generated_at, datetime):
        generated_at = generated_at.astimezone(timezone.utc).isoformat(timespec="minutes")
    generated_at = generated_at or datetime.now(timezone.utc).isoformat(timespec="minutes")

    exclusions = {
        "no_history": 0,
        "insufficient_history": 0,
        "stale_quote": 0,
        "price_outside_range": 0,
        "low_liquidity_or_no_volume": 0,
        "weak_trend": 0,
    }
    candidates = []
    data_dates = []
    sources = set()
    history_count = 0
    quote_count = 0
    metadata = market_data.get("_meta") if isinstance(market_data, dict) else None
    history_errors = (metadata.get("history_errors")
                      if isinstance(metadata, dict) else None) or {
                          "count": 0, "samples": {}
                      }
    # Providers the history refresh's cascade actually needed; None when the
    # batch did not report scope (demo/synthetic data), leaving diagnostics to
    # fall back to the wider provider-name filter.
    history_providers = None
    if isinstance(metadata, dict) and metadata.get("history_providers") is not None:
        history_providers = [str(name)
                             for name in metadata.get("history_providers") or []]

    for spec in universe:
        symbol, name, sector = _spec_parts(spec)
        item = market_data.get(symbol)
        rows, source = _rows_for(item)
        if not rows:
            exclusions["no_history"] += 1
            continue
        history_count += 1
        sources.add(source)
        data_dates.append(rows[-1][0])
        metrics = _metrics(rows)
        if metrics is None:
            exclusions["insufficient_history"] += 1
            continue
        quote_count += 1
        try:
            quote_age = (as_of - date.fromisoformat(metrics["as_of"])).days
        except ValueError:
            quote_age = MAX_QUOTE_AGE_DAYS + 1
        if quote_age < -1 or quote_age > MAX_QUOTE_AGE_DAYS:
            exclusions["stale_quote"] += 1
            continue
        if metrics["close"] < min_price or metrics["close"] >= max_price:
            exclusions["price_outside_range"] += 1
            continue
        adv = metrics["avg_dollar_volume20"]
        if adv is None or adv < min_avg_dollar_volume:
            exclusions["low_liquidity_or_no_volume"] += 1
            continue
        if metrics["ret20_pct"] <= 0 or metrics["close"] <= metrics["sma20"]:
            exclusions["weak_trend"] += 1
            continue

        # Relative ranking only (not a probability, target price, or expected return).
        momentum = (
            0.20 * metrics["ret5_pct"]
            + 0.50 * metrics["ret20_pct"]
            + 0.30 * metrics["ret60_pct"]
        )
        trend_bonus = 2.0 if metrics["close"] > metrics["sma50"] else 0.0
        risk_penalty = (
            min(metrics["vol20_annual_pct"], 100.0) * 0.08
            + abs(min(metrics["drawdown60_pct"], 0.0)) * 0.12
        )
        score = momentum + trend_bonus - risk_penalty
        risk = _risk_label(metrics["vol20_annual_pct"], metrics["drawdown60_pct"])
        reasons = [
            f"20-session momentum {metrics['ret20_pct']:+.1f}%",
            "above 20-day average",
            f"20-day average dollar volume ${adv / 1_000_000:.1f}M",
        ]
        if metrics["close"] > metrics["sma50"]:
            reasons.append("above 50-day average")
        candidates.append({
            "symbol": symbol,
            "name": name,
            "sector": sector,
            **metrics,
            "risk": risk,
            "rank_score": round(score, 2),
            "reasons": reasons,
            "source": source,
            "quote_age_days": quote_age,
        })

    candidates.sort(
        key=lambda c: (c["rank_score"], c["ret20_pct"], c["avg_dollar_volume20"]),
        reverse=True,
    )
    eligible_count = len(candidates)
    candidates = candidates[:limit]
    data_date = max(data_dates) if data_dates else None
    if candidates:
        status = "ok"
        if eligible_count > len(candidates):
            message = (
                f"{eligible_count} stock(s) passed the screen; showing the top "
                f"{len(candidates)} by rank score (display cap {limit})."
            )
        else:
            message = (
                f"Showing all {eligible_count} stock(s) that passed the screen "
                f"(display cap {limit})."
            )
    elif history_count:
        status = "no_candidates"
        message = "No stocks passed all of today’s history, price, liquidity, freshness, and trend filters."
    elif fetch_error:
        status = "unavailable"
        message = "Fresh stock history is unavailable from the configured data sources."
    else:
        status = "waiting"
        message = "Waiting for the first stock-history refresh."
    return {
        "status": status,
        "message": message,
        "generated_at": generated_at,
        "data_date": data_date,
        "source": ", ".join(sorted(sources)) if sources else None,
        "universe_size": len(universe),
        "history_count": history_count,
        "quote_count": quote_count,
        "usable_history_count": quote_count,
        "history_errors": history_errors,
        "history_providers": history_providers,
        "data_date_timezone": "America/New_York",
        "data_date_kind": "daily U.S. market session date; no intraday timestamp",
        "eligible_count": eligible_count,
        "max_candidates": limit,
        "candidates": candidates,
        "exclusions": exclusions,
        "filters": {
            "market": "US-listed watchlist",
            "price_min": min_price,
            "price_max_exclusive": max_price,
            "min_avg_dollar_volume": min_avg_dollar_volume,
            "minimum_sessions": MIN_HISTORY,
            "max_quote_age_days": MAX_QUOTE_AGE_DAYS,
        },
        "method": (
            "Short-term technical watchlist: after price, freshness, liquidity, and trend "
            "filters, relative rank score = 0.20×5-session return + 0.50×20-session "
            "return + 0.30×60-session return + 2 points if close is above the 50-day "
            "average − 0.08×annualized 20-session volatility (%) − 0.12×absolute "
            "60-session drawdown (%). It is a ranking score, not a forecast."
        ),
        "risk_method": (
            "Low: annualized 20-session volatility ≤25% and 60-session drawdown ≥−10%. "
            "Medium: volatility ≤50% and drawdown ≥−20%. Otherwise High. "
            "These are backward-looking buckets, not total or forward risk."
        ),
        "limitations": (
            "Technical history only; no fundamentals, earnings calendar, news, analyst "
            "targets, or intraday data. A share price below $50 does not mean a stock "
            "is cheap. Short-term stock trading can lose money quickly."
        ),
        "fetch_error": str(fetch_error)[:240] if fetch_error else None,
    }


def current_view(screen, *, as_of=None):
    """Suppress cached picks once their quote date is outside the freshness gate."""
    if not isinstance(screen, dict):
        return screen
    today = as_of or date.today()
    view = dict(screen)
    fresh = []
    removed = False
    for candidate in screen.get("candidates") or []:
        item = dict(candidate)
        try:
            age = (today - date.fromisoformat(str(item.get("as_of"))[:10])).days
        except (TypeError, ValueError):
            age = MAX_QUOTE_AGE_DAYS + 1
        if -1 <= age <= MAX_QUOTE_AGE_DAYS:
            item["quote_age_days"] = max(0, age)
            fresh.append(item)
        else:
            removed = True
    view["candidates"] = fresh
    if removed:
        view["eligible_count"] = len(fresh)
    data_date = screen.get("data_date")
    try:
        data_age = (today - date.fromisoformat(str(data_date)[:10])).days if data_date else None
    except (TypeError, ValueError):
        data_age = MAX_QUOTE_AGE_DAYS + 1
    if data_age is not None and data_age > MAX_QUOTE_AGE_DAYS:
        view["status"] = "stale"
        view["message"] = "Cached stock history is stale; waiting for a fresh quote before showing picks."
        view["candidates"] = []
        view["eligible_count"] = 0
    elif removed:
        view["eligible_count"] = len(fresh)
        if fresh:
            view["status"] = "ok"
            view["message"] = "One or more cached picks aged out; only fresh-quote candidates remain."
        else:
            view["status"] = "stale"
            view["message"] = "Cached stock quotes are stale; waiting for a fresh quote before showing picks."
    return view
