"""Best-pick planner — one ranked pick, a buy price, a Malaysian-time window,
and a sell plan expressed as ranges.

What this module is
-------------------
The Stocks tab answers "what passed a technical screen?". This tab answers the
three operational questions that screen leaves open:

1. **Which one?** — a score over the same daily bars, but weighted for a
   *decision* instead of pure momentum: uptrend, a pullback entry rather than
   chasing, calm volatility, contained drawdown, and liquidity.
2. **At what price?** — a *buy zone* (not a single price) built from the
   ticker's own realized volatility: the last close down to a fraction of one
   daily standard deviation below it, never planned below the 20-day average.
3. **When?** — because a U.S.-listed stock is bought during U.S. hours, "when"
   is a **Malaysian-time** window: the regular session is 21:30–04:00 MYT
   (22:30–05:00 when the U.S. is on standard time), and the tab names the
   specific MYT clock window it prefers inside it.
4. **Sell where?** — two take-profit levels at fixed multiples of the plan
   width above the entry, plus a hard stop, each with the MYR value, the
   gross and net-of-cost percentages and the holding range.

What it deliberately is not
---------------------------
No forecast, no expected return, no probability of success, no broker access.
Every level is a *rule* that can be recomputed from the printed formula, and
every range is wide exactly because the underlying data is one daily close per
U.S. session with no intraday book, no fundamentals and no earnings calendar.
The tab states that on the page; `METHOD` / `LIMITATIONS` carry the wording.

The module is pure: it takes plain dicts (the same screen + quote payload the
Stocks tab renders) and returns plain dicts, so it can be unit-tested offline.
"""

from datetime import date, datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo

METHOD = (
    "Score = 0.35×trend (0.2×5-session + 0.5×20-session + 0.3×60-session return) "
    "+ 0.25×buyable-now (1.0 when the current price sits inside the planned buy "
    "zone, falling to 0 as it moves away or under the invalidation line) + "
    "0.20×calm (20-session realized volatility mapped 0→90% to 1→30%) + "
    "0.10×shallow drawdown (60-session, 0→−30% to 1→−5%) + 0.10×liquidity "
    "(log 20-session dollar volume, 0→$3M to 1→$300M). Trend and buyable-now "
    "are relative to the list; the three risk terms use fixed bounds, so their "
    "scores mean the same thing every day. Overall scores are still only "
    "comparable inside the same list."
)
LEVEL_METHOD = (
    "One-session σ = 20-session realized volatility ÷ √252, in dollars at the "
    "last close. Plan width = 2σ, floored at 2.5% and capped at 8% of the entry "
    "price so neither a very quiet nor a very jumpy stock produces an absurd "
    "plan. Buy zone = the last close down to the close − 0.6σ (never below the "
    "20-day average); the reference price is the zone midpoint. Hard stop = "
    "reference − one plan width. Take-profit 1 = reference + 1.4 plan widths, "
    "take-profit 2 = reference + 2.3 plan widths. Reward:risk, percentages and "
    "RM values are all printed from those same numbers. Names above the "
    "configured volatility ceiling get no plan at all, because a stop wide "
    "enough to survive that noise is a loss no normal position should carry. "
    "Setup invalid = zone bottom − 0.5 widths (never below the 20-day average "
    "− 1σ); do-not-chase = zone top + 0.5 widths."
)
LIMITATIONS = (
    "Daily closes only: no intraday order book, bid/ask spread, fundamentals, "
    "earnings calendar, analyst targets or news scoring. The buy zone and both "
    "sell targets are volatility-scaled rules, not predictions — the stock can "
    "gap straight through the stop. Position sizing is yours, and short-term "
    "trading can lose money quickly."
)

MYT = ZoneInfo("Asia/Kuala_Lumpur")
ET = ZoneInfo("America/New_York")

# U.S. regular session in Eastern Time (exchange holidays are not modelled, so
# the page says "weekday" rather than promising a trading day).
_REGULAR_OPEN = time(9, 30)
_REGULAR_CLOSE = time(16, 0)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _num(value):
    """Float or None — never raises, never returns NaN/inf."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _round(value, digits=2):
    number = _num(value)
    return None if number is None else round(number, digits)


def _daily_sigma_pct(vol20_annual_pct):
    """Annualized 20-session volatility → one-session σ in percent."""
    annual = _num(vol20_annual_pct)
    if annual is None or annual <= 0:
        return None
    return annual / math.sqrt(252.0)


def _pct(a, b):
    """Percent change from `b` to `a`."""
    a_num, b_num = _num(a), _num(b)
    if a_num is None or not b_num:
        return None
    return (a_num / b_num - 1.0) * 100.0


def _log10(value):
    number = _num(value)
    if number is None or number <= 0:
        return None
    return math.log10(number)


def _absolute_scale(value, worst, best, invert=False):
    """Map a value onto 0..1 between two fixed, printed bounds.

    Used for the risk terms so their scores mean the same thing in every list.
    """
    number = _num(value)
    if number is None:
        return 0.0
    if invert:                       # lower is better (e.g. volatility)
        hi, lo = worst, best         # `worst` is the larger number here
        if hi == lo:
            return 0.5
        return max(0.0, min(1.0, (hi - number) / (hi - lo)))
    if best == worst:
        return 0.5
    return max(0.0, min(1.0, (number - worst) / (best - worst)))


def _normalise(values):
    """Min-max scale to 0..1 across the list; a flat list becomes all 0.5."""
    clean = [v for v in values if v is not None]
    if not clean:
        return [0.5 for _ in values]
    lo, hi = min(clean), max(clean)
    span = hi - lo
    if span <= 1e-9:
        return [0.5 if v is not None else 0.5 for v in values]
    return [0.5 if v is None else (v - lo) / span for v in values]


# ---------------------------------------------------------------------------
# Malaysian-time session windows
# ---------------------------------------------------------------------------

def _myt_window(open_et, close_et, moment_utc, *, wrap_label=True):
    """Convert an Eastern-Time clock window on the *next* trading weekday.

    Returns the window as MYT times plus the concrete next occurrence, which
    for U.S. hours is normally the following calendar day in Kuala Lumpur.
    """
    reference = moment_utc.astimezone(ET)
    day = reference.date()
    # Find the next weekday whose session has not fully closed yet.
    for step in range(0, 5):
        candidate_day = day + timedelta(days=step)
        if candidate_day.weekday() >= 5:      # Sat/Sun in New York
            continue
        open_et_dt = datetime.combine(candidate_day, open_et, tzinfo=ET)
        close_et_dt = datetime.combine(candidate_day, close_et, tzinfo=ET)
        if close_et_dt <= reference:
            continue
        return {
            "et_date": candidate_day.isoformat(),
            "open_et": open_et_dt,
            "close_et": close_et_dt,
            "open_myt": open_et_dt.astimezone(MYT),
            "close_myt": close_et_dt.astimezone(MYT),
            "wraps_midnight_myt": (open_et_dt.astimezone(MYT).date()
                                   != close_et_dt.astimezone(MYT).date()),
            "label": wrap_label,
        }
    return None


def _fmt_clock(moment):
    """'21:30' — 24-hour clock, unambiguous next to MYT."""
    return moment.strftime("%H:%M")


def _fmt_window(open_myt, close_myt, wraps):
    """'21:30–04:00 MYT (next day)' when the window crosses KL midnight."""
    suffix = " MYT (next day)" if wraps else " MYT"
    return f"{_fmt_clock(open_myt)}–{_fmt_clock(close_myt)}{suffix}"


def session_plan(moment_utc, *, local_tz=None, entry_minutes=90, avoid_minutes=30):
    """When to buy, on a Malaysian clock, for the next U.S. session.

    Rules (kept deliberately simple and printed on the page):

    * A U.S.-listed share only trades in U.S. hours: 09:30–16:00 New York,
      which is 21:30–04:00 in Kuala Lumpur (22:30–05:00 while the U.S. is on
      Eastern Standard Time).
    * The preferred entry window is the first `entry_minutes` of that session —
      deepest liquidity, best chance of a fill inside the planned zone.
    * The final `avoid_minutes` is flagged as a bad time to buy: spreads widen
      into the close and the closing print sets the daily bar the screen uses.
    * Session state is resolved on the New York clock, so "open now" is never
      claimed during the Kuala Lumpur morning.
    """
    local = local_tz or MYT
    moment_utc = moment_utc.astimezone(timezone.utc)
    et_now = moment_utc.astimezone(ET)
    regular = _myt_window(_REGULAR_OPEN, _REGULAR_CLOSE, moment_utc)
    if regular is None:  # pragma: no cover — a 5-day search always finds one
        return None

    open_et, close_et = regular["open_et"], regular["close_et"]
    session_open_now = (et_now.weekday() < 5
                        and open_et <= moment_utc < close_et)
    state = "open" if session_open_now else (
        "weekend" if et_now.weekday() >= 5 else (
            "pre_market" if et_now.date() == regular["et_date"] and
            moment_utc < open_et else "closed"))

    # Entry window for *this* session when it has not passed, otherwise the
    # next one. Both are computed from the same regular-session anchor.
    def window_for(anchor_day):
        day = date.fromisoformat(anchor_day)
        o = datetime.combine(day, _REGULAR_OPEN, tzinfo=ET)
        c = datetime.combine(day, _REGULAR_CLOSE, tzinfo=ET)
        entry_open = o
        entry_close = min(o + timedelta(minutes=entry_minutes), c)
        avoid_open = max(c - timedelta(minutes=avoid_minutes), o)
        return {
            "open": o, "close": c,
            "entry_open": entry_open, "entry_close": entry_close,
            "avoid_open": avoid_open,
            "avoid_close": c,
        }

    today_window = window_for(et_now.date().isoformat()) if et_now.weekday() < 5 else None
    use_today = bool(today_window) and today_window["entry_close"] > moment_utc
    window = today_window if use_today else window_for(regular["et_date"])

    entry_open_myt = window["entry_open"].astimezone(local)
    entry_close_myt = window["entry_close"].astimezone(local)
    avoid_open_myt = window["avoid_open"].astimezone(local)
    avoid_close_myt = window["avoid_close"].astimezone(local)
    open_myt = window["open"].astimezone(local)
    close_myt = window["close"].astimezone(local)
    entry_wraps = entry_open_myt.date() != entry_close_myt.date()
    session_wraps = open_myt.date() != close_myt.date()
    avoid_wraps = avoid_open_myt.date() != avoid_close_myt.date()

    in_entry = window["entry_open"] <= moment_utc <= window["entry_close"]
    minutes_left = (int((window["entry_close"] - moment_utc).total_seconds() // 60)
                    if in_entry else None)
    minutes_to_open = max(0, int((window["entry_open"] - moment_utc).total_seconds() // 60))
    hours_to_open = round(minutes_to_open / 60, 1)

    entry_text = _fmt_window(entry_open_myt, entry_close_myt, entry_wraps)
    if in_entry:
        timing = (f"U.S. session is open now — you are inside the preferred "
                  f"window, which closes in about {minutes_left} min "
                  f"({_fmt_clock(entry_close_myt)} MYT).")
    elif session_open_now:
        timing = (f"The U.S. session is open, but the preferred entry window has "
                  f"closed. Do not chase; the next one is {entry_text}"
                  + (" (the same session closes later tonight)"
                     if avoid_open_myt.date() == moment_utc.astimezone(local).date()
                     else "") + ".")
    elif state == "weekend":
        timing = (f"It is the weekend in New York, so nothing trades. The next "
                  f"preferred window is {entry_text} — about {hours_to_open:g} "
                  f"hours from now.")
    elif state == "pre_market":
        timing = (f"U.S. pre-market is running; regular trading starts at "
                  f"{_fmt_clock(open_myt)} MYT. The best window is the first "
                  f"{entry_minutes} minutes: {entry_text}.")
    else:
        timing = (f"U.S. markets are closed. The next preferred entry window is "
                  f"{entry_text} — about {hours_to_open:g} hours from now.")

    return {
        "state": state,
        "session_open_now": session_open_now,
        "local_tz": str(getattr(local, "key", local)),
        "entry_window": entry_text,
        "entry_open_myt": _fmt_clock(entry_open_myt),
        "entry_close_myt": _fmt_clock(entry_close_myt),
        "entry_window_et": (f"{_fmt_clock(window['entry_open'])}–"
                            f"{_fmt_clock(window['entry_close'])} ET"),
        "entry_date_local": entry_open_myt.date().isoformat(),
        "entry_date_local_label": entry_open_myt.strftime("%a %d %b %Y"),
        "is_today_window": use_today,
        "in_entry_window_now": in_entry,
        "minutes_left_in_entry": minutes_left,
        "hours_to_entry": hours_to_open,
        "session_window_myt": _fmt_window(open_myt, close_myt, session_wraps),
        "avoid_window_myt": _fmt_window(avoid_open_myt, avoid_close_myt, avoid_wraps),
        "timing": timing,
        "runtime_myt": moment_utc.astimezone(local).strftime("%Y-%m-%d %H:%M MYT"),
        "runtime_et": et_now.strftime("%Y-%m-%d %H:%M %Z"),
        "rules": [
            "A U.S.-listed share trades only in U.S. hours, so this is the "
            "Malaysian clock for that U.S. session.",
            "Regular session: 09:30–16:00 New York = 21:30–04:00 Malaysia time "
            "(22:30–05:00 while the U.S. is on Eastern Standard Time, roughly "
            "November to mid-March).",
            "Prefer the first 90 minutes: deepest liquidity and the best chance "
            "of a fill inside the planned zone.",
            "Avoid the final 30 minutes: spreads widen into the close, and the "
            "closing print is the bar this screen scores.",
            "Malaysian and U.S. exchange holidays are not modelled — the window "
            "is the next weekday New York session.",
        ],
    }


# ---------------------------------------------------------------------------
# per-candidate plan
# ---------------------------------------------------------------------------

def _levels(item, *, entry_sigma, target_sigmas, max_stop_pct, cost_pct=0.0,
            min_stop_pct=None, r_multiples=(1.4, 2.3)):
    """Buy zone, sell targets and the hard stop for one candidate.

    The plan is built around one number — the **plan width**, i.e. how far the
    stop sits below the entry:

        plan width = 2 × one-session σ   (σ from 20-session realized volatility)
                     floored at `min_stop_pct` so ordinary intraday noise cannot
                     knock the position out of a quiet stock,
                     capped at `max_stop_pct` so a jumpy stock cannot turn the
                     plan into an unacceptable loss.

    Then: buy zone = the last close and a shallow step below it; stop = entry
    less one plan width; take-profit 1 and 2 = entry plus 1.4 and 2.3 plan
    widths. Every one of those multiples is printed on the page, and each level
    is reported in USD, in indicative RM, as a percentage, and net of the
    estimated round-trip cost.
    """
    close = _num(item.get("close"))
    if close is None or close <= 0:
        return None
    sma20 = _num(item.get("sma20")) or close
    sigma_pct = _daily_sigma_pct(item.get("vol20_annual_pct"))
    if sigma_pct is None or sigma_pct <= 0:
        return None
    sigma_usd = close * sigma_pct / 100.0
    min_stop_pct = 2.0 if min_stop_pct is None else min_stop_pct

    # --- Buy zone: the last close, and a shallow step below it ------------
    step_pct = min(max(entry_sigma * sigma_pct, 0.4), 1.5)
    zone_high = close
    zone_low = close * (1.0 - step_pct / 100.0)
    if sma20 < zone_low:
        # Inside an uptrend the 20-day average is real support; do not plan to
        # buy below it and pretend the trend is intact.
        zone_low = max(sma20, close * (1.0 - 2.0 * step_pct / 100.0))
    zone_low = min(zone_low, zone_high * 0.999)
    entry = (zone_high + zone_low) / 2.0

    # --- Plan width → stop, targets, invalidation -------------------------
    width_pct = min(max(2.0 * sigma_pct, min_stop_pct), max_stop_pct)
    risk_usd = entry * width_pct / 100.0
    stop = entry - risk_usd

    targets = []
    for idx, (r_mult, sigma_mult) in enumerate(
            zip(r_multiples, target_sigmas), 1):
        price = entry + r_mult * risk_usd
        move_pct = _pct(price, entry)
        targets.append({
            "label": f"Take-profit {idx}",
            "price": _round(price),
            "move_pct": _round(move_pct, 2),
            "gross_move_pct": _round(move_pct, 2),
            "net_move_pct": (_round(move_pct - cost_pct, 2)
                             if move_pct is not None else None),
            "sigma": _round(sigma_mult, 1),
            "r_multiple": round(r_mult, 2),
            "note": ("sell half or a third here, trail the rest"
                     if idx == 1 else "final target, or trail the stop"),
        })

    reward_pct = targets[0]["gross_move_pct"]
    rr = round(reward_pct / width_pct, 2) if width_pct > 0 else None
    band = {
        "stop": _round(stop),
        "stop_pct": _round(-width_pct, 2),
        "t1": _round(targets[0]["price"]),
        "t1_pct": targets[0]["gross_move_pct"],
        "t1_net_pct": targets[0]["net_move_pct"],
        "t2": _round(targets[1]["price"]),
        "t2_pct": targets[1]["gross_move_pct"],
        "t2_net_pct": targets[1]["net_move_pct"],
        "rr": rr,
        "width_pct": _round(width_pct, 2),
        "basis": ("expected range for one session is about ±1σ, so the stop "
                  "sits two σ away (with a floor for quiet names)"),
    }

    return {
        "buy_zone_low": _round(zone_low),
        "buy_zone_high": _round(zone_high),
        "buy_reference": _round(entry),
        # Below this before you are in, the setup is gone (0.5 width under the
        # zone, but never under the 20-day average − 1σ, so a healthy pullback
        # inside an intact trend does not read as broken).
        "invalidate_below": _round(max(zone_low - 0.5 * risk_usd,
                                       sma20 - 1.0 * sigma_usd)),
        # Above this the stop is too tight relative to what you would pay.
        "chase_above": _round(zone_high + 0.5 * risk_usd),
        "trend_breaker": _round(sma20 - 0.5 * sigma_usd),
        "sigma_usd": _round(sigma_usd, 3),
        "sigma_pct": _round(sigma_pct, 2),
        "sma20": _round(sma20),
        "targets": targets,
        "stop": band["stop"],
        "stop_pct_from_entry": band["stop_pct"],
        "risk_pct": _round(width_pct, 2),
        "reward_pct_t1": reward_pct,
        "risk_reward": rr,
        "band": band,
        "cost_pct": _round(cost_pct, 2),
    }


def _myr(price, rate):
    """Indicative ringgit value of a USD price (None when FX is unavailable)."""
    price_num, rate_num = _num(price), _num(rate)
    if price_num is None or rate_num is None:
        return None
    return round(price_num * rate_num, 2)


def _attach_myr(levels, rate):
    """Indicative RM equivalents for every level, or Nones without fresh FX."""
    if not levels:
        return levels
    out = dict(levels)
    for key in ("buy_zone_low", "buy_zone_high", "buy_reference", "stop",
                "invalidate_below", "chase_above"):
        out[f"{key}_myr"] = _myr(out.get(key), rate)
    out["targets"] = [
        {**target, "price_myr": _myr(target.get("price"), rate)}
        for target in out.get("targets") or []
    ]
    band = dict(out.get("band") or {})
    for key in ("stop", "t1", "t2"):
        band[f"{key}_myr"] = _myr(band.get(key), rate)
    out["band"] = band
    return out


def _entry_score(item, levels):
    """How buyable the name is *today*: 1 inside the zone, 0 once invalidated.

    This is the honest part of the ranking: a perfect uptrend you can only buy
    by paying well above the plan scores badly, because the stop it implies is
    too tight to survive normal noise.
    """
    price = _num(item.get("quote_price")) or _num(item.get("close"))
    if price is None or price <= 0:
        return 0.0
    zone_low = _num(levels.get("buy_zone_low"))
    zone_high = _num(levels.get("buy_zone_high"))
    invalid = _num(levels.get("invalidate_below"))
    sigma = _num(levels.get("sigma_usd")) or 0.0
    if zone_low is None or zone_high is None or invalid is None:
        return 0.0
    if price <= invalid:
        return 0.0
    if zone_low <= price <= zone_high:
        return 1.0
    if not sigma:
        return 0.0
    if price > zone_high:
        return max(0.0, 1.0 - (price - zone_high) / (2.0 * sigma))
    return max(0.0, 1.0 - (zone_low - price) / (1.5 * sigma))


# ---------------------------------------------------------------------------
# the view
# ---------------------------------------------------------------------------

def build_pick_view(screen, *, now=None, currency_rate=None, demo=False,
                    entry_sigma=None, target_sigmas=None, max_stop_pct=None,
                    hold_sessions=None, weights=None, local_tz=None,
                    cost_pct=None, min_stop_pct=None, max_vol_pct=None):
    """Rank the screened candidates and plan the top one.

    `screen` is the payload app.summary() already builds for the Stocks tab
    (candidates carrying daily metrics, an optional `quote`, and the
    `currency_reference` block). Nothing here re-fetches or re-screens.
    """
    import config  # local import keeps this module importable on its own

    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    entry_sigma = config.PICK_ENTRY_SIGMA if entry_sigma is None else entry_sigma
    target_sigmas = tuple(config.PICK_TARGET_SIGMAS if target_sigmas is None
                          else target_sigmas)
    max_stop_pct = (config.PICK_STOP_MAX_LOSS_PCT if max_stop_pct is None
                    else max_stop_pct)
    hold_sessions = (config.PICK_HOLD_SESSIONS if hold_sessions is None
                     else hold_sessions)
    weights = dict(config.PICK_WEIGHTS if weights is None else weights)
    cost_pct = (config.PICK_COST_PCT if cost_pct is None else cost_pct)
    min_stop_pct = (getattr(config, "PICK_MIN_STOP_PCT", 2.5)
                    if min_stop_pct is None else min_stop_pct)
    max_vol_pct = (getattr(config, "PICK_MAX_VOL_PCT", 120.0)
                   if max_vol_pct is None else max_vol_pct)

    screen = screen if isinstance(screen, dict) else {}
    session = session_plan(moment, local_tz=local_tz)

    currency = (screen.get("currency_reference") or {}) if not demo else {}
    rate = _num(currency_rate)
    if rate is None and currency.get("available"):
        rate = _num(currency.get("rate"))
    if demo:
        rate = None                     # synthetic data never converts to MYR
    fx_note = None
    if demo:
        fx_note = ("RM figures are disabled in DEMO mode: the prices on this "
                   "page are synthetic, so converting them would be meaningless.")
    elif rate is None:
        fx_note = (currency.get("reason")
                   or "No fresh USD/MYR reference, so RM figures are omitted.")

    rows = []
    skipped = 0
    too_volatile = 0
    for raw in screen.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        vol = _num(raw.get("vol20_annual_pct"))
        if vol is not None and vol > max_vol_pct:
            too_volatile += 1
            continue                   # a stop that survives this is unaffordable
        levels = _levels(raw, entry_sigma=entry_sigma,
                         target_sigmas=target_sigmas,
                         max_stop_pct=max_stop_pct, cost_pct=cost_pct,
                         min_stop_pct=min_stop_pct)
        if levels is None:
            skipped += 1                   # no usable close/volatility → no plan
            continue
        rows.append((dict(raw), levels))

    if not rows:
        return {
            "status": str(screen.get("status") or "waiting"),
            "message": (screen.get("message")
                        or "No stock has a usable daily close and volatility yet."),
            "session": session,
            "picks": [],
            "top": None,
            "currency_reference": {
                "available": rate is not None,
                "rate": rate,
                "reason": fx_note,
                "source": currency.get("source"),
                "rate_date": currency.get("rate_date"),
            },
            "weights": weights,
            "cost_pct": cost_pct,
            "min_stop_pct": min_stop_pct,
        "min_stop_pct": min_stop_pct,
            "method": METHOD,
            "level_method": LEVEL_METHOD,
            "limitations": LIMITATIONS,
            "demo": bool(demo),
            "skipped_count": skipped,
            "too_volatile_count": too_volatile,
            "max_vol_pct": max_vol_pct,
        "too_volatile_count": too_volatile,
        "max_vol_pct": max_vol_pct,
            "generated_at": moment.isoformat(timespec="seconds"),
        }

    # ---- score ------------------------------------------------------------
    trend_raw = [
        0.2 * (item.get("ret5_pct") or 0.0)
        + 0.5 * (item.get("ret20_pct") or 0.0)
        + 0.3 * (item.get("ret60_pct") or 0.0)
        for item, _levels_ in rows
    ]
    # Entry quality: can this name be bought today inside its planned zone?
    for item, _levels_ in rows:
        item["quote_price"] = _num((item.get("quote") or {}).get("price")) or _num(
            item.get("close"))
    entry_raw = [_entry_score(item, levels) for item, levels in rows]
    # Trend and buyability are relative to this list (they are the choice between
    # names). The three risk terms are ABSOLUTE, so a violent outlier cannot win
    # just by being the only extreme value in a small list.
    norm = {
        "trend": _normalise(trend_raw),
        "entry": _normalise(entry_raw),
        "calm": [_absolute_scale(item.get("vol20_annual_pct"), 90.0, 30.0, invert=True)
                 for item, _ in rows],
        "drawdown": [_absolute_scale(item.get("drawdown60_pct"), -30.0, -5.0)
                     for item, _ in rows],
        "liquidity": [_absolute_scale(_log10(item.get("avg_dollar_volume20")),
                                      6.5, 8.5)
                      for item, _ in rows],
    }
    total_weight = sum(float(v or 0.0) for v in weights.values()) or 1.0
    ranked = []
    for index, (item, levels) in enumerate(rows):
        parts = {key: norm[key][index] for key in norm}
        score = sum(parts[key] * float(weights.get(key, 0.0))
                    for key in parts) / total_weight
        item["pick_score"] = round(score * 100.0, 1)
        item["score_parts"] = {key: round(value * 100.0, 0)
                               for key, value in parts.items()}
        item["levels"] = _attach_myr(levels, rate)
        item["quote_price"] = _num(item.get("quote_price")) or _num(item.get("close"))
        item["entry_vs_quote_pct"] = (
            _round(_pct(item["levels"].get("buy_reference"),
                        item["quote_price"]), 2)
            if item.get("quote_price") else None)
        item["quote_price_myr"] = _myr(item.get("quote_price"), rate)
        item["flags"] = _quality_flags(item, levels)
        ranked.append(item)

    ranked.sort(key=lambda row: (row["pick_score"],
                                 row.get("ret20_pct") or 0.0), reverse=True)

    top = ranked[0]
    # Every candidate gets the same three-part statement — where its plan sits
    # versus the latest price, how long the first target should take, and the
    # facts behind it — so the ranking table is actionable too, not just the top.
    for row in ranked:
        row_levels = row.get("levels") or {}
        row["hold"] = _hold_estimate(row, row_levels, hold_sessions)
        row["why"] = _why(row)
        row["verdict"], row["now_tag"] = _verdict(row, row_levels)
    # If the top-scoring name is not buyable today, name the closest choice that
    # is. The ranking stays honest (best setup first); this just stops a "wait"
    # headline from hiding the actionable alternative in the table.
    alternative = None
    if top.get("now_tag") in ("wait", "void", "below zone", "slightly high"):
        buyable = [row for row in ranked[1:] if row.get("now_tag") == "in zone"]
        if buyable:
            alternative = buyable[0]
        else:
            closer = sorted(ranked[1:],
                            key=lambda r: -(r.get("score_parts") or {}).get("entry", 0))
            alternative = closer[0] if closer else None
    if alternative is not None:
        top["alternative_symbol"] = alternative.get("symbol")
        top["alternative_name"] = alternative.get("name")
        top["alternative_tag"] = alternative.get("now_tag")
        top["alternative_score"] = alternative.get("pick_score")
        top["alternative_buy"] = (alternative.get("levels") or {}).get("buy_reference")
        top["alternative_stop"] = (alternative.get("levels") or {}).get("stop")
        top["alternative_target"] = (
            ((alternative.get("levels") or {}).get("targets") or [{}])[0]
        ).get("price")
        top["alternative_note"] = (
            f"{alternative.get('symbol')} scores "
            f"{alternative.get('pick_score')} and is "
            f"{'inside its buy zone' if alternative.get('now_tag') == 'in zone' else 'the next-closest to buyable'}."
        )

    status = str(screen.get("status") or "ok")
    if status == "ok" or not status:
        status = "ok"
    return {
        "status": status,
        "message": screen.get("message") or "",
        "session": session,
        "picks": ranked,
        "top": top,
        "data_date": screen.get("data_date"),
        "generated_at": moment.isoformat(timespec="seconds"),
        "screen_generated_at": screen.get("generated_at"),
        "served_from_cache": bool(screen.get("served_from_cache")),
        "refresh_error": screen.get("refresh_error"),
        "currency_reference": {
            "available": rate is not None,
            "rate": rate,
            "reason": fx_note,
            "source": currency.get("source"),
            "rate_date": currency.get("rate_date"),
        },
        "weights": weights,
        "hold_sessions": hold_sessions,
        "entry_sigma": entry_sigma,
        "target_sigmas": list(target_sigmas),
        "max_stop_pct": max_stop_pct,
        "cost_pct": cost_pct,
        "min_stop_pct": min_stop_pct,
        "method": METHOD,
        "level_method": LEVEL_METHOD,
        "limitations": LIMITATIONS,
        "demo": bool(demo),
        "candidate_count": len(ranked),
        "skipped_count": skipped,
        "too_volatile_count": too_volatile,
        "max_vol_pct": max_vol_pct,
    }


def _verdict(item, levels):
    """(sentence, short tag) describing whether the plan is buyable right now."""
    price = _num(item.get("quote_price")) or _num(item.get("close"))
    if price is None or price <= 0:
        return "No price is available, so no entry is possible today.", "no price"
    chase = _num(levels.get("chase_above"))
    low = _num(levels.get("buy_zone_low"))
    high = _num(levels.get("buy_zone_high"))
    invalid = _num(levels.get("invalidate_below"))
    if invalid is not None and price <= invalid:
        return (f"The latest price (${price:,.2f}) is under the invalidation "
                f"line (${invalid:,.2f}) — the plan is void. Do not buy; let the "
                f"screen refresh.", "void")
    if chase is not None and price > chase:
        return (f"The latest price (${price:,.2f}) is above the no-chase line "
                f"(${chase:,.2f}). Wait for a pullback into the zone — paying "
                f"here makes the stop too tight.", "wait")
    if low is not None and high is not None and low <= price <= high:
        return (f"The latest price (${price:,.2f}) is inside the buy zone "
                f"(${low:,.2f}–${high:,.2f}). Buy in the preferred window, "
                f"sized so the stop is a loss you can accept.", "in zone")
    if low is not None and price < low:
        return (f"The latest price (${price:,.2f}) is under the buy zone. Do not "
                f"catch it blind: wait for a close back above ${low:,.2f}, or "
                f"buy a partial position with the stop at "
                f"${_num(levels.get('stop')) or 0:,.2f}.", "below zone")
    if high is not None:
        return (f"The latest price (${price:,.2f}) is just above the buy zone "
                f"(${low:,.2f}–${high:,.2f}) but below the no-chase line. A "
                f"smaller starter position is the honest size here; the stop "
                f"stays at ${_num(levels.get('stop')) or 0:,.2f}.", "slightly high")
    return "The plan needs a price and a volatility estimate; one is missing.", "no plan"


def _quality_flags(item, levels):
    """Human-readable caveats, so a pick never looks cleaner than it is."""
    flags = []
    try:
        age = int(item.get("quote_age_days") or 0)
    except (TypeError, ValueError):
        age = 0
    if age >= 3:
        flags.append(f"Latest daily close is {age} days old.")
    vol = _num(item.get("vol20_annual_pct")) or 0.0
    if vol >= 45:
        flags.append(f"Very volatile ({vol:.0f}% annualized) — expect wide swings.")
    dd = _num(item.get("drawdown60_pct")) or 0.0
    if dd <= -20:
        flags.append(f"Already fell {abs(dd):.0f}% from its 60-session peak.")
    rr = (levels or {}).get("risk_reward")
    if rr is not None and rr < 1.2:
        flags.append(f"Reward/risk at the first target is only {rr:.2f}:1 — "
                     f"the target is too close to be worth the risk after costs.")
    if str(item.get("risk")).lower() == "high":
        flags.append("The Stocks screen labels this name High risk.")
    return flags


def _hold_estimate(item, levels, hold_sessions):
    """How long the first target plausibly takes — always stated as a range."""
    per_day = (_num(item.get("ret20_pct")) or 0.0) / 20.0
    target_pct = (levels or {}).get("reward_pct_t1")
    if not target_pct or per_day <= 0:
        return {
            "sessions": (10, hold_sessions * 2),
            "text": (f"No positive 20-session drift, so time is not on your "
                     f"side: treat this as a 10–{hold_sessions * 2} session idea "
                     f"and let the stop decide."),
        }
    sessions = target_pct / per_day
    low = max(3, int(round(sessions * 0.75)))
    high = max(low + 2, int(round(sessions * 1.75)))
    weeks_low = max(1, round(low / 5))
    weeks_high = max(weeks_low, round(high / 5))
    weeks_text = (f"{weeks_low} week" if weeks_low == weeks_high
                  else f"{weeks_low}–{weeks_high} weeks")
    return {
        "sessions": (low, high),
        "text": (f"At the current 20-session pace ({per_day:+.2f}% a session) the "
                 f"first target is about {low}–{high} U.S. sessions away (roughly "
                 f"{weeks_text} of trading). The stop runs every session in "
                 f"between."),
    }


def _why(item):
    """Short, factual sentences behind the pick — no adjectives it cannot back."""
    bits = []
    ret20 = _num(item.get("ret20_pct"))
    ret60 = _num(item.get("ret60_pct"))
    if ret20 is not None and ret60 is not None:
        bits.append(f"Up {ret20:+.1f}% over 20 sessions and {ret60:+.1f}% over 60.")
    close = _num(item.get("close"))
    sma20 = _num(item.get("sma20"))
    if close and sma20:
        bits.append(f"Close ${close:,.2f} is {(close / sma20 - 1) * 100:+.1f}% "
                    f"versus its 20-day average (${sma20:,.2f}).")
    vol = _num(item.get("vol20_annual_pct"))
    if vol is not None:
        bits.append(f"20-session volatility {vol:.0f}% annualized.")
    adv = _num(item.get("avg_dollar_volume20"))
    if adv:
        bits.append(f"Trades about ${adv / 1_000_000:,.1f}M a day, so the zone "
                    f"should absorb a normal personal order.")
    flags = item.get("flags") or []
    if flags:
        bits.append("Caveats: " + " ".join(flags))
    return bits
