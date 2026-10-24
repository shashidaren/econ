"""Economy 101 — a plain-language read of the US and Malaysian economies.

Input is the same dict ``app.summary()`` already builds: annual World Bank
series for both countries (``economy_history``), the ECB/Frankfurter FX cache
and the FRED Fed-funds cache. No network calls happen here, so a missing source
degrades one card instead of the page.

Every sentence is attached to a number that is printed next to it, and a
missing input says "awaiting data" rather than guessing. Nothing here is a
forecast, a recommendation, or investment advice.
"""

import config

# Indicator keys the tab expects, in reading order.
GROWTH = "gdp_growth"
INFLATION = "inflation"
UNEMPLOYMENT = "unemployment"
TRADE = "trade"

DISCLAIMER = (
    "Descriptive comparison of published annual data, not a forecast and not "
    "investment advice. World Bank annual figures are revised and typically "
    "arrive 6–18 months after the year they describe."
)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _number(value):
    """Return value as a finite float, or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _latest_point(series):
    """(year, value) of the newest observation in a [[year, value], ...] list."""
    best = (None, None)
    for point in series or []:
        try:
            year, value = int(point[0]), _number(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if value is None:
            continue
        if best[0] is None or year >= best[0]:
            best = (year, value)
    return best


def _latest_dated(history):
    """(date, value) of the newest observation in a [(date, value), ...] list."""
    for stamp, value in reversed(history or []):
        number = _number(value)
        if number is not None:
            return (str(stamp)[:10], number)
    return (None, None)


def _clean_series(series):
    """Keep only finite (year, value) pairs, ascending by year."""
    out = []
    for point in series or []:
        try:
            year, value = int(point[0]), _number(point[1])
        except (TypeError, ValueError, IndexError):
            continue
        if value is not None:
            out.append([year, value])
    out.sort(key=lambda item: item[0])
    return out


def _myr_quote(fx_quotes):
    for quote in fx_quotes or []:
        if not isinstance(quote, dict):
            continue
        if str(quote.get("name") or "").strip().endswith("(MYR)"):
            return quote
    return None


# ---------------------------------------------------------------------------
# Rule-based plain-language signals
# ---------------------------------------------------------------------------

def growth_signal(value):
    """Tone + layman text for an annual real GDP growth print."""
    number = _number(value)
    if number is None:
        return ("muted", "No growth print yet. The World Bank series has not "
                         "loaded for this country.")
    if number < 0:
        return ("risk",
                "Output shrank versus a year earlier. Shrinking economies "
                "usually mean fewer jobs, weaker sales and less tax revenue.")
    if number < 1:
        return ("caution",
                "The economy is barely growing — close to standing still, so "
                "small shocks are felt quickly.")
    if number < 2:
        return ("caution",
                "Slow but positive growth. Jobs are still being created, just "
                "not quickly.")
    if number < 4:
        return ("ok",
                "Steady growth: the economy is producing more than a year ago "
                "at a moderate pace.")
    if number < 7:
        return ("ok",
                "Fast growth — output is expanding quickly, which is usually "
                "good for jobs but can also stoke price pressures.")
    return ("caution",
            "Very fast growth. Often export- or commodity-led, and harder to "
            "sustain without overheating.")


def inflation_signal(value):
    """Tone + layman text for annual consumer-price inflation."""
    number = _number(value)
    if number is None:
        return ("muted", "No inflation print yet. The World Bank CPI series "
                         "has not loaded for this country.")
    if number < 0:
        return ("risk",
                "Prices fell over the year (deflation). It sounds welcome, but "
                "it tends to postpone spending and can deepen a slowdown.")
    if number < 2:
        return ("ok",
                "Prices are rising slowly, below the 2% most central banks aim "
                "for. Money keeps most of its purchasing power.")
    if number <= 3:
        return ("ok",
                "Prices are rising at roughly the pace central banks aim for.")
    if number <= 5:
        return ("caution",
                "Prices are rising faster than the 2–3% comfort zone. "
                "Households notice it in food, rent and fuel.")
    return ("risk",
            "High inflation: each month money buys noticeably less, and the "
            "central bank is likely to keep interest rates high to fight it.")


def unemployment_signal(value):
    """Tone + layman text for the unemployment rate."""
    number = _number(value)
    if number is None:
        return ("muted", "No unemployment print yet. The World Bank/ILO "
                         "series has not loaded for this country.")
    if number < 3.5:
        return ("ok",
                "Very low unemployment — jobs are relatively easy to find, "
                "which also pushes wages up.")
    if number < 5.5:
        return ("ok",
                "Unemployment is in a normal range for a developed or "
                "middle-income economy.")
    if number < 8:
        return ("caution",
                "Unemployment is elevated: more people are looking for work "
                "than there are suitable jobs.")
    return ("risk",
            "High unemployment. Household budgets and government support "
            "spending both come under strain.")


def trade_signal(value):
    """Tone + layman text for trade (exports + imports) as a share of GDP."""
    number = _number(value)
    if number is None:
        return ("muted", "No trade print yet. The World Bank trade series has "
                         "not loaded for this country.")
    if number >= 100:
        return ("ok",
                f"Exports plus imports are about {number:.0f}% of the whole "
                "economy — larger than the domestic economy itself. World "
                "demand, shipping costs and border policy move this country "
                "more than its own shoppers do.")
    if number >= 50:
        return ("ok",
                f"Trade is about {number:.0f}% of GDP: an open economy where "
                "exports and imports both matter a lot to jobs and prices.")
    return ("ok",
            f"Trade is about {number:.0f}% of GDP. Domestic spending and "
            "domestic interest rates matter more here than exports do, so "
            "global trade shocks arrive with a delay and a smaller punch.")


SIGNAL_RULES = {
    GROWTH: growth_signal,
    INFLATION: inflation_signal,
    UNEMPLOYMENT: unemployment_signal,
    TRADE: trade_signal,
}


# ---------------------------------------------------------------------------
# Country panels
# ---------------------------------------------------------------------------

def _read_sentence(name, points):
    """One human sentence built only from indicators that actually loaded."""
    year, growth = points.get(GROWTH, (None, None))
    _, inflation = points.get(INFLATION, (None, None))
    _, unemployment = points.get(UNEMPLOYMENT, (None, None))

    clauses = []
    if growth is not None:
        clauses.append(
            f"the economy grew {growth:.1f}%" if growth >= 0
            else f"the economy shrank {abs(growth):.1f}%")
    if inflation is not None:
        clauses.append(
            f"prices rose {inflation:.1f}%" if inflation >= 0
            else f"prices fell {abs(inflation):.1f}%")
    if unemployment is not None:
        clauses.append(f"{unemployment:.1f}% of workers were unemployed")
    if not clauses:
        return (f"Waiting for the World Bank's annual numbers for {name}.")

    when = f"in {year}" if year else "in the latest year reported"
    if len(clauses) == 1:
        joined = clauses[0]
    else:
        joined = ", ".join(clauses[:-1]) + " and " + clauses[-1]
    return f"{when[0].upper()}{when[1:]}, {joined}."


def _policy_for(country, fed_date, fed_value):
    """Policy-rate card for one country.

    The US rate is live (FRED DFF). Malaysia's OPR has no stable key-less
    endpoint, so it is a *manual* config value and is labelled as such — it must
    never look like a live print.
    """
    if country.get("code") == "MY":
        value = _number(config.ECON_MY_OPR)
        return {
            "label": country.get("rate_label") or "Policy rate",
            "value": value,
            "as_of": config.ECON_MY_OPR_AS_OF,
            "source": "manual reference (config.py)",
            "manual": True,
            "link": "https://www.bnm.gov.my/",
            "note": ("Bank Negara Malaysia sets the OPR at its monetary policy "
                     "meetings. This figure is typed into the dashboard config, "
                     "not fetched live — check bnm.gov.my before relying on it."),
        }
    return {
        "label": country.get("rate_label") or "Policy rate",
        "value": fed_value,
        "as_of": fed_date,
        "source": "FRED DFF",
        "manual": False,
        "link": "https://fred.stlouisfed.org/series/DFF",
        "note": ("The rate the US central bank steers; it feeds through to "
                 "credit cards, car loans and variable-rate mortgages."),
    }


def _country_view(country, raw, fed_date, fed_value):
    code = str(country.get("code") or "").upper()
    payload = (raw or {}).get(code) or {}
    raw_series = payload.get("indicators") or {}

    series, latest, stats, signals = {}, {}, [], []
    for meta in config.ECON_INDICATORS:
        key = meta["key"]
        clean = _clean_series(raw_series.get(key))
        series[key] = clean
        year, value = _latest_point(clean)
        if value is not None:
            latest[key] = {"year": year, "value": value}
            stats.append({
                "key": key,
                "label": meta["label"],
                "short": meta["short"],
                "unit": meta["unit"],
                "value": value,
                "year": year,
                "explain": meta["explain"],
            })
        else:
            latest[key] = None
            stats.append({
                "key": key,
                "label": meta["label"],
                "short": meta["short"],
                "unit": meta["unit"],
                "value": None,
                "year": None,
                "explain": meta["explain"],
            })
        tone, text = SIGNAL_RULES[key](value)
        signals.append({
            "key": key,
            "label": meta["label"],
            "tone": tone,
            "value": value,
            "unit": meta["unit"],
            "year": year,
            "text": text,
        })

    points = {key: ((item or {}).get("year"), (item or {}).get("value"))
              for key, item in latest.items()}
    years = [(item or {}).get("year") for item in latest.values()]
    years = [year for year in years if year]
    return {
        "code": code,
        "name": payload.get("name") or country.get("name") or code,
        "currency": country.get("currency"),
        "central_bank": country.get("central_bank"),
        "rate_label": country.get("rate_label"),
        "series": series,
        "latest": latest,
        "stats": stats,
        "signals": signals,
        "read": _read_sentence(
            payload.get("name") or country.get("name") or code, points),
        "policy": _policy_for(country, fed_date, fed_value),
        "latest_year": max(years) if years else None,
        "has_data": bool(years) or _number(fed_value) is not None,
    }


# ---------------------------------------------------------------------------
# Comparisons and transmission channels
# ---------------------------------------------------------------------------

def _by_code(countries):
    return {item["code"]: item for item in countries or []}


def _comparisons(by_code):
    us, my = by_code.get("US"), by_code.get("MY")
    if not us or not my:
        return []

    def side(country, key):
        item = (country.get("latest") or {}).get(key) or {}
        if item.get("value") is None:
            return None
        return item

    out = []
    growth_us, growth_my = side(us, GROWTH), side(my, GROWTH)
    if growth_us and growth_my:
        gap = growth_my["value"] - growth_us["value"]
        out.append({
            "key": GROWTH,
            "label": "Growth",
            "us": growth_us, "my": growth_my,
            "text": (f"Malaysia's latest annual growth ({growth_my['value']:.1f}%, "
                     f"{growth_my['year']}) is {abs(gap):.1f} percentage points "
                     f"{'higher' if gap >= 0 else 'lower'} than the US "
                     f"({growth_us['value']:.1f}%, {growth_us['year']}). Malaysia "
                     "starts from a lower income level, so faster growth is "
                     "normal rather than automatically better."),
        })

    inf_us, inf_my = side(us, INFLATION), side(my, INFLATION)
    if inf_us and inf_my:
        out.append({
            "key": INFLATION,
            "label": "Inflation",
            "us": inf_us, "my": inf_my,
            "text": (f"Prices rose {inf_my['value']:.1f}% in Malaysia "
                     f"({inf_my['year']}) versus {inf_us['value']:.1f}% in the "
                     f"US ({inf_us['year']}). "
                     + ("Malaysian inflation is currently the milder of the two."
                        if inf_my["value"] < inf_us["value"] else
                        "US inflation is currently the milder of the two.")),
        })

    trade_us, trade_my = side(us, TRADE), side(my, TRADE)
    if trade_us and trade_my:
        out.append({
            "key": TRADE,
            "label": "Trade openness",
            "us": trade_us, "my": trade_my,
            "text": (f"Trade is {trade_my['value']:.0f}% of Malaysia's GDP "
                     f"({trade_my['year']}) against {trade_us['value']:.0f}% of "
                     f"the US economy ({trade_us['year']}). Malaysia is far more "
                     "exposed to world trade, so global demand swings hit it "
                     "harder and faster."),
        })

    un_us, un_my = side(us, UNEMPLOYMENT), side(my, UNEMPLOYMENT)
    if un_us and un_my:
        out.append({
            "key": UNEMPLOYMENT,
            "label": "Unemployment",
            "us": un_us, "my": un_my,
            "text": (f"Unemployment is {un_my['value']:.1f}% in Malaysia "
                     f"({un_my['year']}) and {un_us['value']:.1f}% in the US "
                     f"({un_us['year']}). Definitions and benefit systems "
                     "differ, so compare the trend rather than the level."),
        })
    return out


def _ringgit_view(quote):
    """USD/MYR read from the board's ECB/Frankfurter quote."""
    if not quote or _number(quote.get("close")) is None:
        return None
    rate = _number(quote.get("close"))
    spark = [v for v in (quote.get("spark") or []) if _number(v) is not None]
    change = None
    if len(spark) >= 2 and _number(spark[0]):
        change = (rate - float(spark[0])) / float(spark[0]) * 100.0
    return {
        "rate": rate,
        "date": quote.get("date"),
        "source": quote.get("source") or "ECB",
        "spark": [float(v) for v in spark],
        "change_pct": change,
        "stale": bool(quote.get("stale")),
    }


def _connections(by_code, ringgit, fed_value):
    us, my = by_code.get("US") or {}, by_code.get("MY") or {}
    my_trade = (my.get("latest") or {}).get(TRADE) or {}
    us_trade = (us.get("latest") or {}).get(TRADE) or {}
    my_inf = (my.get("latest") or {}).get(INFLATION) or {}
    us_inf = (us.get("latest") or {}).get(INFLATION) or {}
    opr = _number(config.ECON_MY_OPR)
    cards = []

    if my_trade.get("value") is not None:
        cards.append({
            "title": "The US is one of Malaysia's biggest customers",
            "text": ("Malaysia's exports and imports add up to about "
                     f"{my_trade['value']:.0f}% of its economy "
                     f"({my_trade['year']}), against about "
                     f"{us_trade.get('value', 0):.0f}% for the US. Electrical "
                     "and electronic products, palm oil and oil & gas dominate "
                     "Malaysian exports, and the US is a major buyer. When US "
                     "orders slow, Malaysian factories, ports and hiring feel "
                     "it within a quarter or two."),
        })
    if ringgit:
        trend = ""
        if ringgit["change_pct"] is not None:
            trend = (" Over the last ~60 reference days the dollar "
                     f"{'costs more' if ringgit['change_pct'] > 0 else 'buys fewer'} "
                     f"ringgit ({ringgit['change_pct']:+.1f}%), i.e. the ringgit "
                     f"has {'weakened' if ringgit['change_pct'] > 0 else 'strengthened'} "
                     "against the dollar.")
        cards.append({
            "title": "The ringgit is priced in dollars",
            "text": (f"One US dollar is worth about {ringgit['rate']:.3f} ringgit "
                     f"(ECB reference rate, {ringgit['date']}). A higher number "
                     "means a weaker ringgit: imports cost more ringgit, "
                     "exporters earn more ringgit for the same dollar sale." + trend),
        })
    if fed_value is not None and opr is not None:
        gap = fed_value - opr
        cards.append({
            "title": "The interest-rate gap pulls money around",
            "text": (f"The Fed funds rate is {fed_value:.2f}% and Malaysia's OPR "
                     f"is {opr:.2f}%, a gap of {abs(gap):.2f} points with US "
                     f"short-term rates {'higher' if gap >= 0 else 'lower'}. "
                     "Wide gaps can pull global savings toward dollar deposits "
                     "and press the ringgit down, which the central bank has to "
                     "weigh against domestic growth and inflation. Flows also "
                     "depend on risk appetite, so the link is loose, not mechanical."),
        })
    if my_inf.get("value") is not None and us_inf.get("value") is not None:
        cards.append({
            "title": "Inflation travels through trade",
            "text": (f"Malaysia reported {my_inf['value']:.1f}% consumer inflation "
                     f"({my_inf['year']}) and the US {us_inf['value']:.1f}% "
                     f"({us_inf['year']}). Because Malaysia imports a large share "
                     "of what it consumes and prices those imports in dollars, US "
                     "price pressure and a weaker ringgit can both show up in "
                     "Malaysian shop prices even when local demand is calm."),
        })
    cards.append({
        "title": "Energy and palm oil cut both ways",
        "text": ("Malaysia is a net exporter of oil, gas and palm oil, so higher "
                 "energy and vegetable-oil prices lift its export income and "
                 "government revenue while raising costs for Malaysian households "
                 "and firms. The US is a net energy importer overall, so higher "
                 "energy prices mostly land as higher costs for US consumers and "
                 "businesses."),
    })
    cards.append({
        "title": "Chips, AI and the electronics cycle",
        "text": ("Malaysia is a link in the global electronics supply chain "
                 "(packaging and testing of semiconductors plus electrical "
                 "components). Booming US technology demand and AI investment "
                 "help Malaysian exports and factory output; a US tech slowdown "
                 "does the reverse. This is the channel that moves Malaysia's "
                 "trade numbers fastest."),
    })
    cards.append({
        "title": "When investors get nervous, the dollar wins",
        "text": ("In a global risk-off move, money tends to leave smaller "
                 "emerging markets for US dollars and Treasuries. That can push "
                 "the ringgit lower and tighten Malaysian financial conditions "
                 "even when nothing at home has changed — which is why US "
                 "policy and US market mood matter far beyond the US border."),
    })
    return cards


def _households(by_code, ringgit):
    us, my = by_code.get("US") or {}, by_code.get("MY") or {}
    my_inf = ((my.get("latest") or {}).get(INFLATION) or {}).get("value")
    us_inf = ((us.get("latest") or {}).get(INFLATION) or {}).get("value")
    my_trade = ((my.get("latest") or {}).get(TRADE) or {}).get("value")
    us_trade = ((us.get("latest") or {}).get(TRADE) or {}).get("value")

    def clause(value, unit, missing):
        """A factual clause that degrades to a qualitative one, never a guess."""
        if value is None:
            return missing
        return f"{value:,.1f}{unit}"

    myr_note = ""
    if ringgit:
        myr_note = (f" At today's {ringgit['rate']:.3f} ringgit per dollar, "
                    "every 1% the ringgit falls adds roughly 1% to the ringgit "
                    "cost of anything priced in dollars.")

    return {
        "MY": [
            {"title": "Prices in the shops",
             "text": ("Malaysia's inflation rate is the yearly change in an "
                      "average basket of goods, not your own basket — latest "
                      "print "
                      + clause(my_inf, "%", "not loaded yet")
                      + ". Food, fuel and imported electronics can move faster "
                        "than the average.")},
            {"title": "Your loan repayments",
             "text": "Most Malaysian mortgages and many business loans are "
                     "variable-rate and priced off the OPR. When Bank Negara "
                     "moves the OPR, repayments on floating-rate loans tend to "
                     "move in the same direction, with a lag."},
            {"title": "Your job and your employer",
             "text": ("Malaysia is a trade-driven economy: exports plus imports "
                      "equal "
                      + clause(my_trade, "% of GDP",
                               "a large share of GDP")
                      + ". When the US or China buys less, factory orders and "
                        "hiring in Malaysia usually respond before domestic "
                        "spending does.")},
            {"title": "Money you send or spend abroad",
             "text": "The USD/MYR rate sets what a dollar costs you. A weaker "
                     "ringgit makes overseas travel, imported goods and "
                     "dollar-priced subscriptions cost more ringgit, while "
                     "exporters and tourism earn more." + myr_note},
        ],
        "US": [
            {"title": "Prices in the shops",
             "text": ("US inflation is the yearly change in average consumer "
                      "prices — latest print "
                      + clause(us_inf, "%", "not loaded yet")
                      + ". Shelter, transport and food carry the most weight, "
                        "and your own basket can run hotter or cooler.")},
            {"title": "What borrowing costs",
             "text": "The Fed funds rate is the anchor under credit-card, car-"
                     "loan and adjustable mortgage rates. When it stays high, "
                     "new borrowing is expensive and existing variable-rate "
                     "debt costs more each month."},
            {"title": "Your job and your employer",
             "text": ("The US economy is comparatively closed: exports plus "
                      "imports equal "
                      + clause(us_trade, "% of GDP",
                               "a much smaller share of GDP than Malaysia's")
                      + ". Domestic spending and hiring matter more than "
                        "exports, so global trade shocks arrive later and "
                        "softer than they do in Malaysia.")},
            {"title": "The dollar abroad",
             "text": "A stronger dollar buys more when Americans travel or "
                     "import, but it makes US-made goods pricier for foreign "
                     "customers and shrinks the dollar value of overseas profits "
                     "at US multinationals."},
        ],
    }


def _glossary():
    return [
        {"term": "GDP growth",
         "text": "The yearly change in everything a country produces, adjusted "
                 "for inflation. Positive means the economy is expanding."},
        {"term": "Inflation",
         "text": "The yearly change in average consumer prices. It is a rate of "
                 "change, so 2% inflation still means prices are rising, just "
                 "slowly."},
        {"term": "Unemployment rate",
         "text": "The share of people who are available for work and actively "
                 "looking but do not have a job. It misses people who have given "
                 "up looking."},
        {"term": "Policy rate",
         "text": "The interest rate a central bank sets to steer borrowing "
                 "costs. The US uses the Fed funds rate; Malaysia uses the "
                 "Overnight Policy Rate (OPR)."},
        {"term": "USD/MYR",
         "text": "How many ringgit one US dollar buys. The number going up means "
                 "the ringgit is weaker against the dollar."},
        {"term": "Trade openness",
         "text": "Exports plus imports divided by GDP. A high number means the "
                 "outside world shapes the economy strongly."},
        {"term": "Real versus nominal",
         "text": "Real figures strip out price changes; nominal ones do not. All "
                 "growth numbers on this tab are real."},
    ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_economy(sections=None):
    """Assemble the Economy 101 view model from an ``app.summary()`` dict."""
    sections = sections or {}
    raw = sections.get("economy_history") or {}
    raw_countries = raw.get("countries") or {}
    errors = {str(k): str(v) for k, v in (raw.get("errors") or {}).items()}

    fed_date, fed_value = _latest_dated(
        (sections.get("policy_series") or {}).get("DFF") or [])

    countries = [
        _country_view(country, raw_countries, fed_date, fed_value)
        for country in config.ECON_COUNTRIES
    ]
    by_code = _by_code(countries)
    ringgit = _ringgit_view(_myr_quote(sections.get("fx")))

    latest_years = [c["latest_year"] for c in countries if c.get("latest_year")]
    indicator_coverage = {}
    for meta in config.ECON_INDICATORS:
        indicator_coverage[meta["key"]] = {
            "label": meta["label"],
            "countries_with_data": sum(
                1 for c in countries
                if (c["latest"].get(meta["key"]) or {}).get("value") is not None),
            "expected": len(countries),
            "error": errors.get(meta["key"]),
        }

    return {
        "title": "Economy 101",
        "countries": countries,
        "comparisons": _comparisons(by_code),
        "connections": _connections(by_code, ringgit, fed_value),
        "households": _households(by_code, ringgit),
        "ringgit": ringgit,
        "glossary": _glossary(),
        "indicators": [dict(meta) for meta in config.ECON_INDICATORS],
        "coverage": {
            "countries": len(countries),
            "indicators": indicator_coverage,
            "latest_year": max(latest_years) if latest_years else None,
            "history_years": int(config.ECON_HISTORY_YEARS),
            "errors": errors,
            "has_data": any(c.get("has_data") for c in countries),
        },
        "disclaimer": DISCLAIMER,
    }
