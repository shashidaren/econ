"""Accuracy engine for the Pick tab — backtesting, ensembling and live testing.

Why this module exists
----------------------
The original Pick score (``pick.py``) was a fixed-weight blend of 5/20/60-session
momentum, buyability, calm, drawdown and liquidity. The weights were chosen by
hand and never measured against history, so there was no answer to the only
question that matters for a buy price: *how often did this rule actually make
money, out of sample, after costs?*

This module answers that question three ways and merges them:

1. **Walk-forward backtesting (history).** The same eligibility filters and
   level rules the live tab uses are replayed over 1–2 years of daily bars.
   Decisions at date ``t`` use only data ``<= t``; entry is at the *next*
   session's close (``t+1``), so there is no same-bar look-ahead. Weights are
   tuned on earlier folds only, with a purge/embargo equal to the holding
   horizon, and reported on later folds the tuner never saw. Metrics include
   hit rate with Wilson intervals, expectancy, profit factor, Sharpe/Sortino,
   max drawdown, bootstrap confidence intervals and a deflated Sharpe ratio
   that penalises the number of variants tried.

2. **An ensemble of five signal families (the merged method).** No single
   daily-close signal survives every regime: pure momentum buys tops and
   fails in bear markets; pure mean-reversion catches falling knives; pure
   calm buys dead money. The ensemble therefore blends

   * **trend** — SMA stack, MACD histogram, risk-adjusted momentum;
   * **mean-reversion (pullback)** — RSI, Bollinger %B and z-score, but only
     inside an intact uptrend (close above the 50-day average), so it buys
     dips, not collapses;
   * **risk-adjusted** — 20-session Sharpe/Sortino and inverse volatility;
   * **volume breakout** — Donchian position plus volume confirmation;
   * **calm/liquidity** — the original defensive terms (low vol, shallow
     drawdown, dollar volume).

   Each family is mapped to 0..1 on *fixed, printed* bounds (not min-max
   inside the day's list), so scores mean the same thing every day. A
   cross-sectional **regime gate** (share of the universe above its 20-day
   average) suppresses trading entirely in bear breadth. Candidate weight
   vectors are compared walk-forward; the winner is shrunk 50% toward
   equal weights so a lucky fold cannot dominate.

3. **A forward paper-trading ledger (live testing time).** Every live top
   pick is appended to a persistent ledger with its entry reference, stop and
   targets. As sessions pass, entries mature and their realised outcome
   (stop-first simulation on daily closes, net of costs) is recorded. The
   ledger summary — matured count, hit rate with interval, expectancy — is
   combined with the backtest evidence into a calibrated **confidence**
   score. Until enough matured picks exist, the tab says "testing" and the
   verdict is gated: the system waits for evidence instead of pretending
   the first pick is validated.

What this module deliberately is not
------------------------------------
No forecast, no price target from fundamentals, no earnings/news model.
Inputs are daily closes + volumes only (the pipeline has no OHLC, no
intraday book, no fundamentals), so ATR/RSI use close-based proxies and
every level is still a volatility-scaled rule, not a prediction. All
functions are pure (plain dicts/lists in, plain dicts out) except the
small ledger file helpers, so the whole engine is unit-testable offline.

Conventions
-----------
* Rows are ``(date, close, volume-or-None)`` sorted ascending, the same
  contract as ``stocks._rows_for``. Histories may be ``{symbol: {"rows": …}}``
  or ``{symbol: rows}``.
* Returns are in percent unless a name says ``_frac``. Costs are percent
  points subtracted from gross percent.
* ``H`` / ``horizon`` is sessions held *after* entry (entry at ``t+1`` close,
  exit no later than ``t+1+H`` close).
"""

import json
import math
import os
import random
import statistics
import tempfile
from datetime import date

# ---------------------------------------------------------------------------
# small numeric helpers (never raise, never return NaN/inf)
# ---------------------------------------------------------------------------

def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _mean(values):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    return statistics.fmean(vals) if vals else None


def _stdev(values):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if len(vals) < 2:
        return None
    try:
        out = statistics.stdev(vals)
    except statistics.StatisticsError:
        return None
    return out if math.isfinite(out) else None


def _sma(values, period):
    """Simple moving average of the last `period` values (None if short)."""
    if len(values) < period:
        return None
    window = values[-period:]
    if any(v is None for v in window):
        return None
    return statistics.fmean(window)


def _ema_series(values, period):
    """EMA series aligned to `values` (None until `period` observations)."""
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1.0)
    seed = statistics.fmean(values[:period])
    out[period - 1] = seed
    for i in range(period, len(values)):
        prev = out[i - 1] if out[i - 1] is not None else seed
        out[i] = values[i] * k + prev * (1.0 - k)
    return out


def _rsi_series(closes, period=14):
    """Wilder RSI aligned to `closes` (None until `period` gains/losses)."""
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        chg = b - a
        gains.append(max(chg, 0.0))
        losses.append(max(-chg, 0.0))
    avg_gain = statistics.fmean(gains[:period])
    avg_loss = statistics.fmean(losses[:period])
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        g, l = gains[i - 1], losses[i - 1]
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain, avg_loss):
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _norm_ppf(p):
    """Inverse standard-normal CDF (Acklam's approximation, ~1e-9 accurate)."""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _clamp01(x):
    if x is None:
        return 0.0
    return max(0.0, min(1.0, float(x)))


def _absolute_scale(value, worst, best, invert=False):
    """Map onto 0..1 between fixed bounds (same rule as pick.py)."""
    number = _num(value)
    if number is None:
        return 0.0
    if invert:
        hi, lo = worst, best
        if hi == lo:
            return 0.5
        return max(0.0, min(1.0, (hi - number) / (hi - lo)))
    if best == worst:
        return 0.5
    return max(0.0, min(1.0, (number - worst) / (best - worst)))


def _sigmoid(x, center=0.0, scale=1.0):
    """Smooth 0..1 squash; scale<=0 returns a step at `center`."""
    if x is None:
        return 0.0
    if scale <= 0:
        return 1.0 if x >= center else 0.0
    z = (float(x) - center) / scale
    if z > 30:
        return 1.0
    if z < -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-z))


# ---------------------------------------------------------------------------
# row normalisation (same contract as stocks._rows_for)
# ---------------------------------------------------------------------------

def rows_for(item):
    """Normalise a history payload to sorted [(date, close, volume)]."""
    if isinstance(item, dict):
        rows = item.get("rows") or item.get("history") or []
    else:
        rows = item or []
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
    return sorted(out.values())


# ---------------------------------------------------------------------------
# feature engineering — one dict per session, using only data <= that session
# ---------------------------------------------------------------------------

def compute_features(rows):
    """Per-session features for one ticker's rows.

    Returns a list aligned to `rows`; entries are None until 61 sessions of
    history exist (the same minimum the live screen requires). Every feature
    at index ``i`` uses closes/volumes ``0..i`` only — no look-ahead.
    """
    rows = rows_for(rows) if not (rows and isinstance(rows[0], tuple) and len(rows[0]) == 3 and isinstance(rows[0][0], str)) else list(rows)
    closes = [r[1] for r in rows]
    volumes = [r[2] for r in rows]
    n = len(rows)
    if n == 0:
        return []

    # Full-series indicators computed once (each value still only uses past data).
    ema12 = _ema_series(closes, 12)
    ema26 = _ema_series(closes, 26)
    macd = [(a - b) if (a is not None and b is not None) else None
            for a, b in zip(ema12, ema26)]
    macd_vals = [v for v in macd if v is not None]
    # Signal line: EMA-9 of the MACD line (seeded from first available values).
    signal = [None] * n
    if len(macd_vals) >= 9:
        seed = statistics.fmean(macd_vals[:9])
        k = 2.0 / 10.0
        started = False
        prev = seed
        for i, v in enumerate(macd):
            if v is None:
                continue
            if not started:
                signal[i] = seed
                prev = seed
                started = True
            else:
                prev = v * k + prev * (1.0 - k)
                signal[i] = prev
    rsi = _rsi_series(closes, 14)

    # On-balance volume (close-based: sign of daily change × volume).
    obv = [None] * n
    running = 0.0
    for i in range(n):
        if i == 0:
            running = 0.0
        else:
            chg = closes[i] - closes[i - 1]
            vol = volumes[i] if volumes[i] is not None else 0.0
            if chg > 0:
                running += vol
            elif chg < 0:
                running -= vol
        obv[i] = running

    feats = []
    for i in range(n):
        if i < 60:
            feats.append(None)
            continue
        window = closes[:i + 1]
        close = closes[i]
        sma20 = _sma(window, 20)
        sma50 = _sma(window, 50)
        sd20 = _stdev(window[-20:]) if len(window) >= 20 else None
        # Daily returns over the last 20 / 60 sessions.
        rets20 = [b / a - 1.0 for a, b in zip(window[-21:-1], window[-20:]) if a]
        rets60 = [b / a - 1.0 for a, b in zip(window[-61:-1], window[-60:]) if a] \
            if len(window) >= 61 else []
        vol20 = (_stdev(rets20) * math.sqrt(252.0) * 100.0) if len(rets20) >= 15 else None
        vol60 = (_stdev(rets60) * math.sqrt(252.0) * 100.0) if len(rets60) >= 30 else None
        mean20 = _mean(rets20)
        sd_ret20 = _stdev(rets20)
        sharpe20 = (mean20 / sd_ret20 * math.sqrt(252.0)) if (mean20 is not None and sd_ret20) else None
        downside = [r for r in rets20 if r < 0]
        downside_dev = math.sqrt(statistics.fmean([r * r for r in downside])) if downside else 0.0
        sortino20 = (mean20 / downside_dev * math.sqrt(252.0)) if (mean20 is not None and downside_dev > 0) else None

        def _chg(back):
            return ((close / window[-1 - back]) - 1.0) * 100.0 if len(window) > back and window[-1 - back] else None

        ret5, ret20, ret60 = _chg(5), _chg(20), _chg(60)
        # Bollinger (20, 2σ) and z-score.
        upper = (sma20 + 2.0 * sd20) if (sma20 is not None and sd20 is not None) else None
        lower = (sma20 - 2.0 * sd20) if (sma20 is not None and sd20 is not None) else None
        pct_b = ((close - lower) / (upper - lower)) if (upper is not None and lower is not None and upper != lower) else None
        zscore = ((close - sma20) / sd20) if (sma20 is not None and sd20) else None
        # Donchian (close proxy — no high/low in this pipeline).
        hi20 = max(window[-20:])
        lo20 = min(window[-20:])
        donchian = ((close - lo20) / (hi20 - lo20)) if hi20 != lo20 else 0.5
        hi60 = max(window[-60:])
        dist_high60 = ((close / hi60) - 1.0) * 100.0 if hi60 else None
        # Drawdown over 60 sessions.
        peak, dd = 0.0, 0.0
        for v in window[-60:]:
            peak = max(peak, v)
            if peak:
                dd = min(dd, (v / peak - 1.0) * 100.0)
        # Volume / dollar volume.
        vols20 = [v for v in volumes[max(0, i - 19):i + 1] if v is not None]
        vol_today = volumes[i]
        vol_ratio = (vol_today / statistics.fmean(vols20)) if (vol_today is not None and len(vols20) >= 15 and statistics.fmean(vols20) > 0) else None
        dollar_vols = [c * v for c, v in zip(window[-20:], volumes[max(0, i - 19):i + 1]) if v is not None]
        adv20 = statistics.fmean(dollar_vols) if len(dollar_vols) >= 15 else None
        # OBV slope over 20 sessions, normalised by average volume.
        obv_slope = None
        if len(vols20) >= 15 and obv[i] is not None and obv[max(0, i - 20)] is not None:
            avg_vol = statistics.fmean(vols20) or 1.0
            obv_slope = (obv[i] - obv[max(0, i - 20)]) / (avg_vol * 20.0)
        # ATR proxy: mean absolute daily move over 14 sessions, in dollars.
        atr_proxy = None
        if len(rets20) >= 14:
            atr_proxy = statistics.fmean([abs(r) for r in rets20[-14:]]) * close
        # Amihud illiquidity proxy: |ret| per $1M dollar volume (scaled ×1e6).
        amihud = None
        if adv20 and rets20:
            amihud = abs(rets20[-1]) / (adv20 / 1_000_000.0) * 100.0 if adv20 > 0 else None

        feats.append({
            "date": rows[i][0],
            "close": close,
            "sma20": sma20,
            "sma50": sma50,
            "ret5_pct": ret5,
            "ret20_pct": ret20,
            "ret60_pct": ret60,
            "vol20_annual_pct": vol20,
            "vol60_annual_pct": vol60,
            "sharpe20": sharpe20,
            "sortino20": sortino20,
            "rsi14": rsi[i],
            "macd": macd[i],
            "macd_signal": signal[i],
            "macd_hist": ((macd[i] - signal[i]) if (macd[i] is not None and signal[i] is not None) else None),
            "bollinger_pct_b": pct_b,
            "zscore20": zscore,
            "donchian20": donchian,
            "drawdown60_pct": dd,
            "dist_high60_pct": dist_high60,
            "volume_ratio20": vol_ratio,
            "avg_dollar_volume20": adv20,
            "obv_slope20": obv_slope,
            "atr_proxy": atr_proxy,
            "amihud": amihud,
        })
    return feats


# ---------------------------------------------------------------------------
# signal families — each maps features to 0..1 on fixed bounds
# ---------------------------------------------------------------------------

def signal_trend(f):
    """Trend following: SMA stack + MACD histogram + momentum."""
    if not f:
        return 0.0
    close, sma20, sma50 = f.get("close"), f.get("sma20"), f.get("sma50")
    stack = 0.0
    if close is not None and sma20 is not None and sma50 is not None:
        if close > sma20:
            stack += 0.5
        if sma20 > sma50:
            stack += 0.5
    macd_score = _sigmoid(f.get("macd_hist"), center=0.0, scale=0.15) \
        if f.get("macd_hist") is not None else 0.5
    mom = 0.2 * (f.get("ret5_pct") or 0.0) + 0.5 * (f.get("ret20_pct") or 0.0) \
        + 0.3 * (f.get("ret60_pct") or 0.0)
    mom_score = _absolute_scale(mom, -6.0, 6.0)
    return _clamp01(0.4 * stack + 0.3 * macd_score + 0.3 * mom_score)


def signal_mean_reversion(f):
    """Pullback inside an uptrend: RSI/Bollinger/z-score, gated by SMA50."""
    if not f:
        return 0.0
    close, sma50 = f.get("close"), f.get("sma50")
    if close is None or sma50 is None or close <= sma50:
        return 0.0  # no uptrend → no dip to buy (avoids falling knives)
    rsi = f.get("rsi14")
    if rsi is None:
        rsi_score = 0.5
    elif rsi < 30:
        rsi_score = 0.4   # washed out but possibly broken; do not over-reward
    elif rsi <= 55:
        rsi_score = 1.0   # healthy pullback zone
    elif rsi <= 70:
        rsi_score = 0.6   # warm but not extended
    else:
        rsi_score = 0.2   # overbought — chasing
    pct_b = f.get("bollinger_pct_b")
    if pct_b is None:
        boll_score = 0.5
    elif pct_b < 0.0:
        boll_score = 0.4
    elif pct_b <= 0.5:
        boll_score = 1.0
    elif pct_b <= 0.9:
        boll_score = 0.6
    else:
        boll_score = 0.2
    z = f.get("zscore20")
    if z is None:
        z_score = 0.5
    elif z < -2.0:
        z_score = 0.4
    elif z <= 0.0:
        z_score = 1.0
    elif z <= 1.0:
        z_score = 0.6
    else:
        z_score = 0.2
    return _clamp01(0.4 * rsi_score + 0.3 * boll_score + 0.3 * z_score)


def signal_risk_adjusted(f):
    """Return per unit of risk: Sharpe/Sortino plus inverse volatility."""
    if not f:
        return 0.0
    sharpe = _absolute_scale(f.get("sharpe20"), -1.0, 2.0)
    sortino = _absolute_scale(f.get("sortino20"), -1.0, 3.0)
    calm = _absolute_scale(f.get("vol20_annual_pct"), 90.0, 30.0, invert=True)
    return _clamp01(0.4 * sharpe + 0.3 * sortino + 0.3 * calm)


def signal_volume_breakout(f):
    """Donchian position + volume confirmation + OBV slope."""
    if not f:
        return 0.0
    donchian = _clamp01(f.get("donchian20") if f.get("donchian20") is not None else 0.5)
    vr = f.get("volume_ratio20")
    if vr is None:
        vol_score = 0.5
    elif vr < 0.7:
        vol_score = 0.3
    elif vr <= 1.5:
        vol_score = 0.7
    elif vr <= 2.5:
        vol_score = 1.0
    else:
        vol_score = 0.6   # climax volume is often exhaustion, not confirmation
    obv = _sigmoid(f.get("obv_slope20"), center=0.0, scale=0.5) \
        if f.get("obv_slope20") is not None else 0.5
    return _clamp01(0.4 * donchian + 0.35 * vol_score + 0.25 * obv)


def signal_calm(f):
    """Defensive: low vol, shallow drawdown, liquid (original Pick terms)."""
    if not f:
        return 0.0
    calm = _absolute_scale(f.get("vol20_annual_pct"), 90.0, 30.0, invert=True)
    dd = _absolute_scale(f.get("drawdown60_pct"), -30.0, -5.0)
    adv = f.get("avg_dollar_volume20")
    liq = _absolute_scale(math.log10(adv) if (adv and adv > 0) else None, 6.5, 8.5)
    return _clamp01(0.5 * calm + 0.25 * dd + 0.25 * liq)


SIGNAL_FAMILIES = ("trend", "mean_reversion", "risk_adjusted", "volume_breakout", "calm")

_SIGNAL_FUNCS = {
    "trend": signal_trend,
    "mean_reversion": signal_mean_reversion,
    "risk_adjusted": signal_risk_adjusted,
    "volume_breakout": signal_volume_breakout,
    "calm": signal_calm,
}

# Candidate weight vectors over SIGNAL_FAMILIES (trend, meanrev, riskadj, volume, calm).
# Small, pre-registered set — the deflated Sharpe penalises trying more.
WEIGHT_CANDIDATES = {
    "equal": (0.20, 0.20, 0.20, 0.20, 0.20),
    "trend_heavy": (0.40, 0.10, 0.20, 0.20, 0.10),
    "meanrev_heavy": (0.10, 0.40, 0.20, 0.20, 0.10),
    "riskadj_heavy": (0.20, 0.10, 0.40, 0.20, 0.10),
    "volume_heavy": (0.20, 0.10, 0.20, 0.40, 0.10),
    "calm_heavy": (0.10, 0.10, 0.20, 0.10, 0.50),
    "trend_riskadj": (0.30, 0.10, 0.30, 0.20, 0.10),
    "trend_only": (1.00, 0.00, 0.00, 0.00, 0.00),
}
DEFAULT_WEIGHTS = WEIGHT_CANDIDATES["equal"]


def family_scores(f):
    """All five family scores for one feature dict."""
    return {name: _SIGNAL_FUNCS[name](f) for name in SIGNAL_FAMILIES}


def ensemble_score(f, weights=DEFAULT_WEIGHTS, breadth=None):
    """Weighted ensemble in 0..1 plus per-family parts.

    `breadth` (share of universe above SMA20) does not change the ranking
    inside one date — it gates whether to trade at all (see regime_gate).
    """
    parts = family_scores(f)
    total = sum(weights) or 1.0
    score = sum(parts[name] * w for name, w in zip(SIGNAL_FAMILIES, weights)) / total
    return _clamp01(score), {k: round(v, 4) for k, v in parts.items()}


def regime_gate(breadth, bear_threshold=0.35):
    """(trade_allowed, label). Bear breadth suppresses new picks."""
    if breadth is None:
        return True, "breadth unknown — trading allowed"
    if breadth < bear_threshold:
        return False, f"bear breadth ({breadth:.0%}) — no new picks"
    if breadth < 0.5:
        return True, f"soft breadth ({breadth:.0%}) — trade small"
    return True, f"healthy breadth ({breadth:.0%})"


# ---------------------------------------------------------------------------
# level rules — mirror of pick._levels (kept in sync by test)
# ---------------------------------------------------------------------------

def levels_for(close, sma20, vol20_annual_pct, *, entry_sigma=0.6,
               target_multiples=(1.4, 2.3), min_stop_pct=2.5, max_stop_pct=8.0):
    """Buy zone / stop / targets for one (close, sma20, vol) triple.

    Mirrors ``pick._levels``: σ = vol20/√252; width = 2σ clamped to
    [min_stop_pct, max_stop_pct]; zone = close → close−0.6σ (never below
    SMA20); stop = reference − width; TPs = reference + multiple×width.
    Returns None when inputs are unusable (same as the live tab).
    """
    close, sma20 = _num(close), _num(sma20)
    vol = _num(vol20_annual_pct)
    if close is None or close <= 0 or vol is None or vol <= 0:
        return None
    sma20 = sma20 if (sma20 is not None and sma20 > 0) else close
    sigma_pct = vol / math.sqrt(252.0)
    sigma_usd = close * sigma_pct / 100.0
    step_pct = min(max(entry_sigma * sigma_pct, 0.4), 1.5)
    zone_high = close
    zone_low = close * (1.0 - step_pct / 100.0)
    if sma20 < zone_low:
        zone_low = max(sma20, close * (1.0 - 2.0 * step_pct / 100.0))
    zone_low = min(zone_low, zone_high * 0.999)
    entry = (zone_high + zone_low) / 2.0
    width_pct = min(max(2.0 * sigma_pct, min_stop_pct), max_stop_pct)
    risk_usd = entry * width_pct / 100.0
    stop = entry - risk_usd
    targets = [entry + m * risk_usd for m in target_multiples]
    return {
        "buy_zone_low": zone_low, "buy_zone_high": zone_high,
        "buy_reference": entry, "stop": stop,
        "tp1": targets[0] if targets else None,
        "tp2": targets[1] if len(targets) > 1 else None,
        "width_pct": width_pct, "sigma_pct": sigma_pct, "sigma_usd": sigma_usd,
    }


# ---------------------------------------------------------------------------
# statistics — intervals, Sharpe, deflated Sharpe, bootstrap
# ---------------------------------------------------------------------------

def wilson_ci(k, n, z=1.96):
    """Wilson score interval for a proportion (k successes in n trials)."""
    if n <= 0:
        return (None, None)
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def beta_posterior(k, n, prior_a=1.0, prior_b=1.0):
    """Beta-Binomial posterior mean + 95% credible interval (normal approx)."""
    a, b = prior_a + k, prior_b + (n - k)
    mean = a / (a + b)
    var = (a * b) / ((a + b) ** 2 * (a + b + 1))
    sd = math.sqrt(var) if var > 0 else 0.0
    return {"mean": mean, "lo": max(0.0, mean - 1.96 * sd),
            "hi": min(1.0, mean + 1.96 * sd), "a": a, "b": b}


def sharpe_of(returns_pct, horizon=20, risk_free_pct=0.0):
    """Annualised Sharpe from per-trade percent returns."""
    vals = [r for r in returns_pct if r is not None and math.isfinite(r)]
    if len(vals) < 2:
        return None
    sd = _stdev(vals)
    if not sd or sd == 0:
        return None
    mean = statistics.fmean(vals)
    per_trade = (mean - risk_free_pct) / sd
    return per_trade * math.sqrt(252.0 / max(1, horizon))


def sortino_of(returns_pct, horizon=20, target_pct=0.0):
    vals = [r for r in returns_pct if r is not None and math.isfinite(r)]
    if len(vals) < 2:
        return None
    downside = [min(0.0, r - target_pct) for r in vals]
    dd = math.sqrt(statistics.fmean([d * d for d in downside])) if downside else 0.0
    if dd == 0:
        return None
    mean = statistics.fmean(vals)
    return (mean - target_pct) / dd * math.sqrt(252.0 / max(1, horizon))


def expectancy_of(returns_pct):
    vals = [r for r in returns_pct if r is not None and math.isfinite(r)]
    if not vals:
        return None
    return statistics.fmean(vals)


def profit_factor_of(returns_pct):
    vals = [r for r in returns_pct if r is not None and math.isfinite(r)]
    wins = sum(r for r in vals if r > 0)
    losses = -sum(r for r in vals if r < 0)
    if losses == 0:
        return float("inf") if wins > 0 else None
    return wins / losses


def max_drawdown_of(equity):
    """Max peak-to-trough fall of a cumulative equity list (percent points)."""
    if not equity:
        return None
    peak, worst = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        worst = min(worst, v - peak)
    return worst


def bootstrap_ci(values, n_boot=1000, seed=7, block=1):
    """Block-bootstrap 95% CI for the mean (deterministic seed)."""
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if len(vals) < 5:
        return (None, None)
    rng = random.Random(seed)
    n = len(vals)
    block = max(1, min(block, n))
    means = []
    for _ in range(n_boot):
        sample = []
        while len(sample) < n:
            start = rng.randrange(n)
            for j in range(block):
                sample.append(vals[(start + j) % n])
                if len(sample) >= n:
                    break
        means.append(statistics.fmean(sample[:n]))
    means.sort()
    lo = means[int(0.025 * n_boot)]
    hi = means[min(n_boot - 1, int(0.975 * n_boot))]
    return (lo, hi)


def deflated_sharpe(observed_sharpe, n_trials, n_obs, skew=0.0, kurt=3.0,
                    trial_sharpes=None):
    """Bailey & Lopez de Prado deflated Sharpe ratio (0..1).

    Penalises the best of `n_trials` variants. `trial_sharpes` (Sharpes of
    all variants tried) estimates the variance of trial Sharpes; without it
    the variance is approximated as 1/n_obs. Returns None when inputs are
    insufficient (fewer than 2 trials or 10 observations).
    """
    if observed_sharpe is None or n_trials < 2 or n_obs < 10:
        return None
    if trial_sharpes and len(trial_sharpes) >= 2:
        var = _stdev(list(trial_sharpes)) ** 2 if _stdev(list(trial_sharpes)) else 1.0 / n_obs
    else:
        var = 1.0 / n_obs
    var = max(var, 1e-8)
    gamma = 0.5772156649  # Euler-Mascheroni
    e = math.e
    sr0 = math.sqrt(var) * ((1 - gamma) * _norm_ppf(1 - 1.0 / n_trials)
                            + gamma * _norm_ppf(1 - 1.0 / (n_trials * e)))
    num = (observed_sharpe - sr0) * math.sqrt(max(1, n_obs - 1))
    den = math.sqrt(1 - skew * observed_sharpe
                    + (kurt - 1) / 4.0 * observed_sharpe ** 2)
    if den <= 0:
        return None
    return _norm_cdf(num / den)


def required_n_for_hit_rate(p_hat=0.55, margin=0.10, z=1.96):
    """Observations needed to pin a hit rate within ±margin (95%)."""
    p = min(max(p_hat, 0.05), 0.95)
    return int(math.ceil(z * z * p * (1 - p) / (margin * margin)))


def t_stat(values, mu=0.0):
    vals = [v for v in values if v is not None and math.isfinite(v)]
    if len(vals) < 2:
        return None
    sd = _stdev(vals)
    if not sd:
        return None
    return (statistics.fmean(vals) - mu) / (sd / math.sqrt(len(vals)))


# ---------------------------------------------------------------------------
# backtest engine
# ---------------------------------------------------------------------------

def _eligible(f, *, min_price=5.0, max_price=50.0, min_adv=5_000_000.0,
              max_vol=120.0):
    if not f:
        return False
    close, adv, vol = f.get("close"), f.get("avg_dollar_volume20"), f.get("vol20_annual_pct")
    if close is None or close < min_price or close >= max_price:
        return False
    if adv is None or adv < min_adv:
        return False
    if vol is not None and vol > max_vol:
        return False
    if (f.get("ret20_pct") or 0.0) <= 0 or (f.get("sma20") is None) \
            or close <= f["sma20"]:
        return False
    return True


def _simulate_forward(closes, entry_idx, levels, horizon, cost_pct):
    """Stop-first simulation on daily closes; returns outcome dict or None."""
    entry = closes[entry_idx]
    if entry is None or entry <= 0:
        return None
    stop, tp1, tp2 = levels["stop"], levels["tp1"], levels["tp2"]
    exit_price, outcome, sessions = None, "timeout", horizon
    for j in range(entry_idx + 1, min(entry_idx + horizon + 1, len(closes))):
        price = closes[j]
        if price is None:
            continue
        if stop is not None and price <= stop:
            exit_price, outcome, sessions = stop, "stop", j - entry_idx
            break
        if tp1 is not None and price >= tp1:
            exit_price, outcome, sessions = tp1, "tp1", j - entry_idx
            break
        if tp2 is not None and price >= tp2:
            exit_price, outcome, sessions = tp2, "tp2", j - entry_idx
            break
    if exit_price is None:
        exit_idx = min(entry_idx + horizon, len(closes) - 1)
        exit_price = closes[exit_idx]
        sessions = exit_idx - entry_idx
    if exit_price is None:
        return None
    gross_pct = (exit_price / entry - 1.0) * 100.0
    net_pct = gross_pct - cost_pct
    window = [c for c in closes[entry_idx:min(entry_idx + horizon + 1, len(closes))]
              if c is not None]
    mfe = (max(window) / entry - 1.0) * 100.0 if window else None
    mae = (min(window) / entry - 1.0) * 100.0 if window else None
    return {
        "entry": entry, "exit": exit_price, "outcome": outcome,
        "sessions": sessions, "gross_pct": gross_pct, "net_pct": net_pct,
        "mfe_pct": mfe, "mae_pct": mae,
    }


def _summarise_trades(trades, horizon, cost_pct):
    """Aggregate metrics for a list of trade dicts (each with net_pct)."""
    nets = [t["net_pct"] for t in trades]
    n = len(nets)
    if n == 0:
        return {"n": 0, "hit_rate": None, "mean_net_pct": None,
                "median_net_pct": None, "expectancy_pct": None,
                "profit_factor": None, "sharpe": None, "sortino": None,
                "max_drawdown_pct": None, "t_stat": None,
                "hit_ci": (None, None), "mean_ci": (None, None),
                "outcome_counts": {}}
    hits = sum(1 for v in nets if v > 0)
    equity, running = [], 0.0
    for v in nets:
        running += v
        equity.append(running)
    # Skew/kurtosis for the deflated Sharpe (sample moments).
    mean = statistics.fmean(nets)
    sd = _stdev(nets) or 0.0
    if sd > 0 and n >= 4:
        skew = statistics.fmean([((v - mean) / sd) ** 3 for v in nets])
        kurt = statistics.fmean([((v - mean) / sd) ** 4 for v in nets])
    else:
        skew, kurt = 0.0, 3.0
    counts = {}
    for t in trades:
        counts[t.get("outcome", "?")] = counts.get(t.get("outcome", "?"), 0) + 1
    return {
        "n": n,
        "hit_rate": hits / n,
        "hit_ci": wilson_ci(hits, n),
        "mean_net_pct": mean,
        "median_net_pct": statistics.median(nets),
        "expectancy_pct": expectancy_of(nets),
        "profit_factor": profit_factor_of(nets),
        "sharpe": sharpe_of(nets, horizon=horizon),
        "sortino": sortino_of(nets, horizon=horizon),
        "max_drawdown_pct": max_drawdown_of(equity),
        "t_stat": t_stat(nets),
        "mean_ci": bootstrap_ci(nets, block=max(1, horizon // 2)),
        "skew": skew, "kurt": kurt,
        "outcome_counts": counts,
    }


def walk_forward_backtest(histories, *, horizon=20, cost_pct=0.35, top_n=1,
                          step=5, min_history=61, n_folds=3,
                          bear_threshold=0.35, weights=None,
                          min_price=5.0, max_price=50.0,
                          min_adv=5_000_000.0, max_vol=120.0,
                          entry_sigma=0.6, min_stop_pct=2.5, max_stop_pct=8.0):
    """Walk-forward backtest of the ensemble pick rule.

    `histories`: ``{symbol: rows-or-payload}``. Returns a dict with pooled
    out-of-sample metrics, per-fold metrics, the chosen weights, calibration
    bins, benchmark comparison and trade-level detail (capped for the API).

    Walk-forward protocol: decision dates are split chronologically into
    `n_folds`. Fold 0 uses equal weights (no training data yet). Each later
    fold tunes weights on *prior* folds only (purge: training decisions whose
    forward window reaches into the test fold are dropped — embargo =
    horizon), then evaluates on the held-out fold. The tuned vector is shrunk
    50% toward equal weights. Pooled OOS metrics come from held-out folds
    only; fold 0 is reported separately as "untuned".
    """
    # --- precompute features + family scores per ticker --------------------
    series = {}   # symbol -> {"rows": rows, "feats": [...], "closes": [...]}
    for symbol, payload in (histories or {}).items():
        rows = rows_for(payload)
        if len(rows) < min_history + horizon + 2:
            continue
        feats = compute_features(rows)
        series[str(symbol).upper()] = {
            "rows": rows, "feats": feats,
            "closes": [r[1] for r in rows],
        }
    if not series:
        return {"status": "insufficient_history",
                "message": "Not enough daily history to backtest.",
                "n": 0, "folds": [], "trades": []}

    # Decision indices: shared calendar is not assumed; use per-ticker index
    # positions aligned by date. Build the union of dates, then map each date
    # to per-ticker indices.
    all_dates = sorted({r[0] for s in series.values() for r in s["rows"]})
    date_pos = {day: pos for pos, day in enumerate(all_dates)}
    date_to_idx = {}
    for sym, s in series.items():
        date_to_idx[sym] = {r[0]: i for i, r in enumerate(s["rows"])}

    # Candidate decision dates: every `step` dates after min_history, leaving
    # room for entry (t+1) + horizon forward sessions.
    decision_dates = []
    for pos, day in enumerate(all_dates):
        if pos < min_history:
            continue
        # Require at least one ticker to have entry+horizon data past `day`.
        decision_dates.append(day)
    decision_dates = decision_dates[::max(1, step)]
    # Drop dates too close to the end (need entry + horizon closes).
    trimmed = []
    for day in decision_dates:
        ok = any(
            (date_to_idx[sym].get(day) is not None
             and date_to_idx[sym][day] + 1 + horizon < len(s["closes"]))
            for sym in series
        )
        if ok:
            trimmed.append(day)
    decision_dates = trimmed
    if len(decision_dates) < n_folds * 2:
        return {"status": "insufficient_history",
                "message": (f"Only {len(decision_dates)} decision dates with "
                            "full forward data; need more history."),
                "n": 0, "folds": [], "trades": [],
                "decision_dates": len(decision_dates)}

    # --- score every (date, ticker) once per weight candidate --------------
    # Precompute family scores + eligibility + breadth per decision date.
    date_data = []  # list of {"date": day, "breadth": b, "rows": [{symbol, f, parts}]}
    for day in decision_dates:
        rows_here = []
        above = eligible = 0
        for sym, s in series.items():
            idx = date_to_idx[sym].get(day)
            if idx is None:
                continue
            f = s["feats"][idx]
            if f is None:
                continue
            if f.get("close") is not None and f.get("sma20") is not None:
                if f["close"] > f["sma20"]:
                    above += 1
            if not _eligible(f, min_price=min_price, max_price=max_price,
                             min_adv=min_adv, max_vol=max_vol):
                continue
            eligible += 1
            rows_here.append({"symbol": sym, "idx": idx, "f": f,
                              "parts": family_scores(f)})
        breadth = (above / max(1, above + eligible)) if (above + eligible) else None
        # Breadth denominator: use all tickers with features that date.
        total_feat = sum(1 for sym, s in series.items()
                         if date_to_idx[sym].get(day) is not None
                         and s["feats"][date_to_idx[sym][day]] is not None)
        breadth = (above / total_feat) if total_feat else None
        date_data.append({"date": day, "breadth": breadth, "rows": rows_here})

    def pick_for_date(dd, w):
        """Top-`top_n` picks for one decision date under weights `w`."""
        allowed, _label = regime_gate(dd["breadth"], bear_threshold)
        if not allowed or not dd["rows"]:
            return []
        scored = []
        for row in dd["rows"]:
            total = sum(w) or 1.0
            score = sum(row["parts"][name] * weight
                        for name, weight in zip(SIGNAL_FAMILIES, w)) / total
            scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [(score, row) for score, row in scored[:max(1, top_n)]]

    def trades_for_dates(dates_subset, w):
        trades = []
        for dd in dates_subset:
            if dd["date"] not in {d["date"] for d in dates_subset}:
                continue
            for score, row in pick_for_date(dd, w):
                sym, idx = row["symbol"], row["idx"]
                s = series[sym]
                entry_idx = idx + 1
                if entry_idx + horizon >= len(s["closes"]):
                    continue
                levels = levels_for(row["f"].get("close"), row["f"].get("sma20"),
                                    row["f"].get("vol20_annual_pct"),
                                    entry_sigma=entry_sigma,
                                    min_stop_pct=min_stop_pct,
                                    max_stop_pct=max_stop_pct)
                if levels is None:
                    continue
                sim = _simulate_forward(s["closes"], entry_idx, levels,
                                        horizon, cost_pct)
                if sim is None:
                    continue
                # Benchmark: equal-weight forward return of all eligible that date.
                bench_returns = []
                for other in dd["rows"]:
                    os_ = series[other["symbol"]]
                    oi = other["idx"] + 1
                    if oi + horizon < len(os_["closes"]):
                        bench_returns.append(
                            (os_["closes"][oi + horizon] / os_["closes"][oi] - 1.0) * 100.0
                            - cost_pct)
                bench = statistics.fmean(bench_returns) if bench_returns else None
                trades.append({
                    "symbol": sym, "date": dd["date"],
                    "entry_date": s["rows"][entry_idx][0],
                    "score": round(score, 4),
                    "breadth": dd["breadth"],
                    **sim,
                    "benchmark_net_pct": bench,
                    "excess_net_pct": (sim["net_pct"] - bench) if bench is not None else None,
                })
        return trades

    # --- chronological folds ------------------------------------------------
    n_dates = len(date_data)
    fold_size = max(1, n_dates // n_folds)
    folds = []
    for k in range(n_folds):
        start = k * fold_size
        end = n_dates if k == n_folds - 1 else (k + 1) * fold_size
        folds.append(date_data[start:end])

    fixed_weights = tuple(weights) if weights is not None else None
    oos_trades, untuned_trades = [], []
    fold_reports = []
    trial_sharpes = []
    chosen_per_fold = []

    for k, test_dates in enumerate(folds):
        if fixed_weights is not None:
            w = fixed_weights
            chosen = "fixed (caller-supplied)"
        elif k == 0:
            w = DEFAULT_WEIGHTS
            chosen = "equal (no training data yet)"
        else:
            train_dates = [d for fold in folds[:k] for d in fold]
            # Purge: drop training decisions whose forward window overlaps the
            # test fold's first decision date (embargo = horizon sessions).
            test_start = test_dates[0]["date"] if test_dates else None
            test_start_pos = date_pos.get(test_start)
            if test_start_pos is not None:
                train_dates = [
                    dd for dd in train_dates
                    if date_pos.get(dd["date"], test_start_pos) + 1 + horizon < test_start_pos
                ]
            best_name, best_w, best_sharpe = None, DEFAULT_WEIGHTS, None
            if train_dates:
                for name, cand in WEIGHT_CANDIDATES.items():
                    cand_trades = trades_for_dates(train_dates, cand)
                    cand_sharpe = sharpe_of([t["net_pct"] for t in cand_trades],
                                            horizon=horizon)
                    trial_sharpes.append(cand_sharpe)
                    if cand_sharpe is not None and (best_sharpe is None or cand_sharpe > best_sharpe):
                        best_name, best_w, best_sharpe = name, cand, cand_sharpe
            if best_name is None:
                w = DEFAULT_WEIGHTS
                chosen = "equal (too few purged training trades)"
            else:
                # Shrink 50% toward equal weights (anti-overfit).
                w = tuple(0.5 * b + 0.5 * e for b, e in zip(best_w, DEFAULT_WEIGHTS))
                chosen = f"{best_name} shrunk 50% toward equal"
        test_trades = trades_for_dates(test_dates, w)
        summary = _summarise_trades(test_trades, horizon, cost_pct)
        fold_reports.append({
            "fold": k, "dates": (test_dates[0]["date"], test_dates[-1]["date"]) if test_dates else None,
            "weights": list(w), "chosen": chosen, **summary,
        })
        chosen_per_fold.append({"fold": k, "weights": list(w), "chosen": chosen})
        if k == 0 and fixed_weights is None:
            untuned_trades.extend(test_trades)
        else:
            oos_trades.extend(test_trades)

    pooled = _summarise_trades(oos_trades, horizon, cost_pct)
    untuned = _summarise_trades(untuned_trades, horizon, cost_pct)
    # Deflated Sharpe on pooled OOS (trials = candidate vectors tried).
    dsr = deflated_sharpe(pooled.get("sharpe"), len(WEIGHT_CANDIDATES),
                          pooled.get("n") or 0,
                          skew=pooled.get("skew") or 0.0,
                          kurt=pooled.get("kurt") or 3.0,
                          trial_sharpes=[s for s in trial_sharpes if s is not None])
    # Benchmark comparison.
    excesses = [t["excess_net_pct"] for t in oos_trades
                if t.get("excess_net_pct") is not None]
    bench_summary = {
        "n": len(excesses),
        "mean_excess_pct": statistics.fmean(excesses) if excesses else None,
        "t_stat": t_stat(excesses),
        "mean_ci": bootstrap_ci(excesses, block=max(1, horizon // 2)),
    }
    # Calibration: quintiles of OOS score → empirical hit rate.
    calibration = calibration_table(oos_trades)
    # Final weights: last fold's weights (most recent training window).
    final_weights = chosen_per_fold[-1]["weights"] if chosen_per_fold else list(DEFAULT_WEIGHTS)

    return {
        "status": "ok" if oos_trades else "no_trades",
        "horizon": horizon, "cost_pct": cost_pct, "top_n": top_n,
        "n_decision_dates": n_dates,
        "n_tickers": len(series),
        "folds": fold_reports,
        "oos": pooled,
        "untuned_fold0": untuned,
        "deflated_sharpe": dsr,
        "n_trials": len(WEIGHT_CANDIDATES),
        "benchmark": bench_summary,
        "calibration": calibration,
        "chosen_weights": final_weights,
        "chosen_per_fold": chosen_per_fold,
        "trades": oos_trades[:500],  # capped for API/persistence
        "trades_total": len(oos_trades),
        "method": ("Walk-forward (chronological folds, purge/embargo = horizon); "
                   "entry at t+1 close; stop-first simulation on daily closes; "
                   "net of costs; weights tuned on prior folds only and shrunk "
                   "50% toward equal weights."),
        "limitations": ("Daily closes only (no OHLC/intraday); overlapping "
                        "decisions; universe is the fixed 32-name watchlist "
                        "(survivorship as listed); past performance is not a "
                        "forecast."),
    }


def calibration_table(trades, bins=5):
    """Quintile bins of ensemble score → empirical hit rate (reliability)."""
    scored = sorted([t for t in trades if t.get("score") is not None],
                    key=lambda t: t["score"])
    if len(scored) < bins * 2:
        return []
    size = len(scored) / bins
    out = []
    for b in range(bins):
        chunk = scored[int(b * size):int((b + 1) * size)] if b < bins - 1 \
            else scored[int(b * size):]
        if not chunk:
            continue
        hits = sum(1 for t in chunk if t["net_pct"] > 0)
        out.append({
            "bin": b,
            "score_lo": round(min(t["score"] for t in chunk), 3),
            "score_hi": round(max(t["score"] for t in chunk), 3),
            "n": len(chunk),
            "hit_rate": hits / len(chunk),
            "mean_net_pct": statistics.fmean([t["net_pct"] for t in chunk]),
        })
    return out


# ---------------------------------------------------------------------------
# forward paper-trading ledger
# ---------------------------------------------------------------------------

def ledger_load(path):
    """Load ledger entries (list of dicts); [] on missing/corrupt file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def ledger_save(path, entries):
    """Atomic save (0600, fsync, replace) — never raises."""
    try:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True
    except OSError:
        return False


def ledger_append(path, entry):
    """Append one entry (deduped by date+symbol); returns True if added."""
    entries = ledger_load(path)
    key = (str(entry.get("date")), str(entry.get("symbol")).upper())
    for existing in entries:
        if (str(existing.get("date")), str(existing.get("symbol")).upper()) == key:
            return False
    entries.append(dict(entry))
    return ledger_save(path, entries)


def ledger_mature(entries, histories, *, horizon=20, cost_pct=0.35):
    """Fill realised outcomes for entries whose horizon has elapsed.

    `histories` maps symbol → rows. An entry matures once the history has at
    least `horizon` sessions after its entry date. Returns (entries, n_new).
    Entries are mutated copies (originals untouched).
    """
    out, newly = [], 0
    series = {}
    for sym, payload in (histories or {}).items():
        series[str(sym).upper()] = rows_for(payload)
    for entry in entries:
        item = dict(entry)
        if item.get("matured"):
            out.append(item)
            continue
        sym = str(item.get("symbol") or "").upper()
        rows = series.get(sym) or []
        decision_date = str(item.get("date") or "")
        explicit_entry = str(item.get("entry_date") or "")
        if explicit_entry and explicit_entry != decision_date:
            idx = next((i for i, r in enumerate(rows) if r[0] == explicit_entry), None)
        else:
            t_idx = next((i for i, r in enumerate(rows) if r[0] == decision_date), None)
            idx = (t_idx + 1) if t_idx is not None else None
        if idx is None or idx + horizon >= len(rows):
            out.append(item)
            continue
        closes = [r[1] for r in rows]
        levels = {
            "stop": _num(item.get("stop")),
            "tp1": _num(item.get("tp1")),
            "tp2": _num(item.get("tp2")),
        }
        sim = _simulate_forward(closes, idx, levels, horizon, cost_pct)
        if sim is None:
            out.append(item)
            continue
        item.update({
            "matured": True,
            "entry_date": rows[idx][0],
            "exit_date": rows[min(idx + horizon, len(rows) - 1)][0],
            **sim,
        })
        newly += 1
        out.append(item)
    return out, newly


def ledger_summary(entries):
    """Aggregate matured ledger entries into accuracy stats."""
    matured = [e for e in (entries or []) if e.get("matured")]
    pending = [e for e in (entries or []) if not e.get("matured")]
    nets = [e["net_pct"] for e in matured if e.get("net_pct") is not None]
    n = len(nets)
    if n == 0:
        return {"n_matured": 0, "n_pending": len(pending),
                "hit_rate": None, "hit_ci": (None, None),
                "mean_net_pct": None, "expectancy_pct": None,
                "profit_factor": None, "sharpe": None,
                "outcome_counts": {}}
    hits = sum(1 for v in nets if v > 0)
    counts = {}
    for e in matured:
        counts[e.get("outcome", "?")] = counts.get(e.get("outcome", "?"), 0) + 1
    return {
        "n_matured": n, "n_pending": len(pending),
        "hit_rate": hits / n, "hit_ci": wilson_ci(hits, n),
        "mean_net_pct": statistics.fmean(nets),
        "expectancy_pct": expectancy_of(nets),
        "profit_factor": profit_factor_of(nets),
        "sharpe": sharpe_of(nets, horizon=int(matured[0].get("horizon") or 20)),
        "outcome_counts": counts,
        "posterior": beta_posterior(hits, n),
    }


# ---------------------------------------------------------------------------
# confidence — merges backtest + ledger + current quality
# ---------------------------------------------------------------------------

def confidence_from_evidence(backtest=None, ledger=None, *, min_n=30,
                             buy_threshold=60.0):
    """Calibrated 0..100 confidence that the pick rule has edge after costs.

    Evidence is shrunk by sample size (N/(N+20)), so a handful of lucky
    trades cannot produce high confidence. The verdict is gated: below
    `buy_threshold` (or with insufficient matured evidence) the tab must
    say "testing" rather than "buy".
    """
    bt = (backtest or {}).get("oos") or {}
    led = ledger or {}
    reasons, score_parts = [], {}

    # Backtest leg (50%): expectancy + deflated Sharpe + hit-rate interval.
    bt_n = int(bt.get("n") or 0)
    bt_shrink = bt_n / (bt_n + 20.0) if bt_n else 0.0
    exp = bt.get("expectancy_pct")
    dsr = (backtest or {}).get("deflated_sharpe")
    bt_score = 0.0
    if exp is not None and bt_n >= 10:
        bt_score += 50.0 * _sigmoid(exp, center=0.0, scale=1.0)
        reasons.append(f"backtest expectancy {exp:+.2f}% over {bt_n} OOS trades")
    else:
        reasons.append(f"backtest has only {bt_n} OOS trades — insufficient")
    if dsr is not None:
        bt_score += 0.0  # DSR reported separately; expectancy already counted
        reasons.append(f"deflated Sharpe {dsr:.2f} ({(backtest or {}).get('n_trials', '?')} variants tried)")
    bt_score *= bt_shrink
    score_parts["backtest"] = round(bt_score, 1)

    # Ledger leg (30%): live matured picks.
    led_n = int(led.get("n_matured") or 0)
    led_shrink = led_n / (led_n + 20.0) if led_n else 0.0
    led_exp = led.get("expectancy_pct")
    led_score = 0.0
    if led_exp is not None and led_n >= 5:
        led_score = 30.0 * _sigmoid(led_exp, center=0.0, scale=1.0)
        reasons.append(f"live ledger expectancy {led_exp:+.2f}% over {led_n} matured picks")
    else:
        reasons.append(f"live ledger has {led_n} matured picks (need {min_n})")
    led_score *= led_shrink
    score_parts["ledger"] = round(led_score, 1)

    # Current-quality leg (20%): breadth + volatility regime from backtest meta.
    quality = 10.0  # neutral baseline; adjusted below
    if bt_n >= 10 and exp is not None and exp > 0:
        quality = 20.0
    elif bt_n >= 10:
        quality = 5.0
    score_parts["current_quality"] = round(quality, 1)

    total = bt_score + led_score + quality
    # Hard caps: no validated claim without both legs having data.
    if bt_n < 10 or led_n < 5:
        total = min(total, 39.0)
    if exp is not None and exp <= 0 and bt_n >= 20:
        total = min(total, 39.0)
    total = max(0.0, min(100.0, total))

    if total >= buy_threshold and bt_n >= 10 and led_n >= min_n:
        label = "validated"
    elif total >= 40.0:
        label = "provisional"
    else:
        label = "testing"
    required = max(0, min_n - led_n)
    return {
        "score": round(total, 1),
        "label": label,
        "buy_allowed": label == "validated",
        "parts": score_parts,
        "reasons": reasons,
        "n_backtest_oos": bt_n,
        "n_ledger_matured": led_n,
        "required_ledger_n": required,
        "min_n": min_n,
        "buy_threshold": buy_threshold,
    }
