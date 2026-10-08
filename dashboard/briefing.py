"""Investment briefing — a glanceable read of data already on the board.

No new network calls. Every line is derived from the same cache the panels
use, so a missing source degrades one card instead of the page. This is a
read of the board, not a forecast and not investment advice.
"""


def _latest(hist):
    if not hist:
        return None
    for _d, v in reversed(hist):
        if v is not None:
            return v
    return None


def _quotes(items):
    return [q for q in (items or []) if q and q.get("close") is not None]


def _by_name(quotes, *needles):
    for q in quotes:
        name = (q.get("name") or "").lower()
        if any(n in name for n in needles):
            return q
    return None


def _cpi_row(rows):
    for r in rows or []:
        code = (r.get("code") or "").upper()
        name = (r.get("name") or "").lower()
        if code in ("US", "USA") or "united states" in name:
            return r
    return None


def _card(cid, label, tone, value, text):
    return {
        "id": cid,
        "label": label,
        "tone": tone,  # risk | caution | ok | muted
        "value": value,
        "text": text,
    }


def build_briefing(sections):
    """Return posture + signal cards from a summary() dict."""
    sections = sections or {}
    indices = _quotes(sections.get("indices"))
    commodities = _quotes(sections.get("commodities"))
    fx = _quotes(sections.get("fx"))
    cpi = sections.get("cpi") or []
    gdp = [r for r in (sections.get("gdp") or []) if r.get("value") is not None]
    policy = sections.get("policy_series") or {}
    curve = _latest(sections.get("curve"))
    be = _latest(sections.get("breakeven"))
    fed = _latest(policy.get("DFF"))
    ecb = _latest(policy.get("ECBDFR"))

    cards = []
    risk = 0
    caution = 0

    if curve is None:
        cards.append(_card(
            "curve", "Yield curve", "muted", "—",
            "10y–2y spread not loaded. Recession read unavailable."))
    elif curve < 0:
        risk += 2
        cards.append(_card(
            "curve", "Yield curve", "risk", f"{curve:.2f}%",
            "Inverted. Classic late-cycle warning — quality and duration "
            "over cyclical beta until the spread turns positive."))
    elif curve < 0.50:
        caution += 1
        cards.append(_card(
            "curve", "Yield curve", "caution", f"{curve:.2f}%",
            "Flat, not inverted. Expansion still priced, but little cushion "
            "if growth disappoints."))
    else:
        cards.append(_card(
            "curve", "Yield curve", "ok", f"{curve:.2f}%",
            "Positively sloped. No inversion signal on the 10y–2y spread."))

    if fed is None and be is None:
        cards.append(_card(
            "policy", "Policy", "muted", "—",
            "Fed funds and breakeven not loaded."))
    else:
        bits = []
        tone = "ok"
        if fed is not None:
            bits.append(f"Fed funds {fed:.2f}%")
        if ecb is not None:
            bits.append(f"ECB deposit {ecb:.2f}%")
        if fed is not None and be is not None:
            real = fed - be
            bits.append(f"real-rate proxy {real:.2f}% (funds − 10y breakeven)")
            if real >= 1.5:
                tone = "caution"
                caution += 1
                tail = "Restrictive vs market inflation. Headwind for duration-sensitive growth."
            elif real < 0:
                tone = "caution"
                caution += 1
                tail = "Policy below breakeven. Easier real rates — watch inflation re-acceleration."
            else:
                tail = "Real-rate proxy is neither deeply restrictive nor easy."
        else:
            tail = "Need both Fed funds and breakeven for a real-rate proxy."
        cards.append(_card(
            "policy", "Policy", tone,
            f"{fed:.2f}%" if fed is not None else "—",
            " · ".join(bits) + ". " + tail))

    us = _cpi_row(cpi)
    if us is None and be is None:
        cards.append(_card(
            "inflation", "Inflation", "muted", "—",
            "CPI and breakeven not loaded."))
    else:
        tone = "ok"
        parts = []
        if us is not None:
            parts.append(f"US CPI {us['value']:+.1f}% ({us.get('year') or '?'})")
            if us["value"] >= 4:
                tone = "risk"
                risk += 1
            elif us["value"] >= 3:
                tone = "caution"
                caution += 1
        if be is not None:
            parts.append(f"10y breakeven {be:.2f}%")
            if be >= 3 and tone == "ok":
                tone = "caution"
                caution += 1
        hot = tone != "ok"
        cards.append(_card(
            "inflation", "Inflation", tone,
            f"{us['value']:+.1f}%" if us is not None else f"{be:.2f}%",
            " · ".join(parts) + (
                ". Still hot versus a 2% anchor." if hot
                else ". Near a 2% anchor on the series we have.")))

    if not gdp:
        cards.append(_card(
            "growth", "Growth", "muted", "—",
            "World Bank GDP rows not loaded."))
    else:
        neg = [r for r in gdp if r["value"] < 0]
        soft = [r for r in gdp if r["value"] < 1]
        us_g = next((r for r in gdp if (r.get("code") or "").upper() in ("US", "USA")
                     or "united states" in (r.get("name") or "").lower()), None)
        tone = "ok"
        if len(neg) >= 2 or (us_g and us_g["value"] < 0):
            tone = "risk"
            risk += 1
        elif soft:
            tone = "caution"
            caution += 1
        names = ", ".join(r["name"] for r in neg[:3]) or "none"
        us_bit = f"US {us_g['value']:+.1f}%. " if us_g else ""
        cards.append(_card(
            "growth", "Growth", tone,
            f"{us_g['value']:+.1f}%" if us_g else f"{len(neg)} neg",
            f"{us_bit}{len(neg)}/{len(gdp)} economies contracting "
            f"({names}); {len(soft)}/{len(gdp)} below 1% real GDP."))

    up = sum(1 for q in indices if (q.get("chg") or 0) > 0)
    down = sum(1 for q in indices if (q.get("chg") or 0) < 0)
    populated = up + down + sum(1 for q in indices if (q.get("chg") or 0) == 0)
    expected = len(sections.get("indices") or []) or populated
    if not indices:
        cards.append(_card(
            "markets", "Markets", "muted", "—",
            "No index quotes loaded."))
    else:
        breadth = up / populated if populated else 0
        tone = "ok" if breadth >= 0.6 else ("risk" if breadth < 0.4 else "caution")
        if tone == "risk":
            risk += 1
        elif tone == "caution":
            caution += 1
        oil = _by_name(commodities, "brent", "wti", "crude")
        gold = _by_name(commodities, "gold")
        extra = []
        if oil and oil.get("chg_pct") is not None:
            extra.append(f"{oil['name'].split('—')[0].strip()} {oil['chg_pct']:+.1f}%")
        if gold and gold.get("chg_pct") is not None:
            extra.append(f"gold {gold['chg_pct']:+.1f}%")
        if oil and gold and (oil.get("chg") or 0) > 0 and (gold.get("chg") or 0) > 0:
            extra.append("oil and gold both up — inflation-watch, not a clean risk-on")
            if tone == "ok":
                tone = "caution"
                caution += 1
        usd_up = sum(1 for q in fx if (q.get("chg") or 0) > 0)
        if fx:
            extra.append(
                f"USD {'firmer' if usd_up >= len(fx) / 2 else 'softer'} "
                f"({usd_up}/{len(fx)} USD crosses up)")
        gap = expected - populated
        if gap:
            extra.append(f"{gap} index card(s) awaiting data — breadth is partial")
        cards.append(_card(
            "markets", "Markets", tone, f"{up}/{populated} up",
            f"Populated indices {up} up / {down} down. " + " · ".join(extra) + "."))

    if risk >= 2 or (curve is not None and curve < 0 and caution):
        posture, tone = "Defensive", "risk"
        headline = (
            "Late-cycle caution. The curve or the growth/inflation mix is "
            "unfriendly — treat equity strength as a trade, not a regime.")
    elif risk or caution >= 2:
        posture, tone = "Cautious", "caution"
        headline = (
            "Mixed board. No single alarm, but policy, inflation, or breadth "
            "is not a clean all-clear.")
    elif not cards or all(c["tone"] == "muted" for c in cards):
        posture, tone = "Awaiting data", "muted"
        headline = "Not enough live series to read a posture yet."
    else:
        posture, tone = "Constructive", "ok"
        headline = (
            "Curve, growth, and breadth are not flashing stress. "
            "Still a macro board, not a buy signal.")

    return {
        "posture": posture,
        "tone": tone,
        "headline": headline,
        "cards": cards,
        "disclaimer": (
            "Read of this board's own data. Not a forecast and not investment advice."),
    }
