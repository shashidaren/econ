"""Dashboard configuration — edit this file to change what appears on the board.

Everything here is plain data: symbol lists, country lists, cache lifetimes.
No logic lives in this file on purpose, so it is safe to tweak freely.
"""

import os

# --- Server -----------------------------------------------------------------
HOST = os.environ.get("ECON_HOST", "0.0.0.0")
PORT = int(os.environ.get("ECON_PORT", "8080"))

# ECON_DEMO=1 serves bundled sample data instead of hitting the network
# (used for offline previews / layout testing).
DEMO = os.environ.get("ECON_DEMO", "") in ("1", "true", "yes")

# Background refresh cadence (seconds). Page itself auto-reloads every 5 min.
REFRESH_SECONDS = int(os.environ.get("ECON_REFRESH", "300"))

# Persistent cache file on disk so service restarts keep last known good data.
CACHE_FILE = os.environ.get("ECON_CACHE_FILE", "/var/tmp/econ-dashboard-cache.json")

# --- Panels -----------------------------------------------------------------

# Global equity indices (primary key = Stooq symbol; sources.py cascades
# automatically across Yahoo Finance -> FRED -> Stooq).
INDICES = [
    ("S&P 500",            "^spx"),
    ("Nasdaq 100",         "^ndx"),
    ("Dow Jones 30",       "^dji"),
    ("Euro Stoxx 50",      "^stx"),
    ("DAX (Germany)",      "^dax"),
    ("FTSE 100 (UK)",      "^ukx"),
    ("Nikkei 225 (JP)",    "^nkx"),
    ("Shanghai Composite", "^shc"),
    ("Hang Seng (HK)",     "^hsi"),
]

# Commodities — the pulse of the real economy (cascades across
# Yahoo Finance -> FRED -> Stooq -> CoinGecko PAXG for gold).
COMMODITIES = [
    ("WTI Crude",   "cl.f"),
    ("Brent Crude", "cb.f"),
    ("Gold",        "xauusd"),
    ("Silver",      "xagusd"),
    ("Copper",      "hg.f"),
    ("Wheat",       "zw.f"),
]

# FRED quote fallback series (label, FRED series id, max_age_days before card
# shows a "stale" badge). Used automatically by sources.py when market quote
# endpoints are blocked or rate-limited.
FRED_QUOTES = [
    ("S&P 500",                   "SP500",        10),
    ("NASDAQ 100",                "NASDAQ100",    10),
    ("Dow Jones 30",              "DJIA",         10),
    ("Nikkei 225 (JP)",           "NIKKEI225",    10),
    ("WTI Crude",                 "DCOILWTI",     10),
    ("Brent Crude",               "DCOILBRENTEU", 10),
    ("Copper — global (monthly)", "PCOPPUSDM",    55),
    ("Wheat — global (monthly)",  "PWHEAMTUSDM",  55),
]

# Currencies, USD base, via ECB reference rates (frankfurter.app)
FX_CURRENCIES = [
    ("Euro",              "EUR"),
    ("Japanese Yen",      "JPY"),
    ("Chinese Yuan",      "CNY"),
    ("British Pound",     "GBP"),
    ("Australian Dollar", "AUD"),
    ("Swiss Franc",       "CHF"),
    ("Indian Rupee",      "INR"),
    ("Malaysian Ringgit", "MYR"),
]

# World Bank macro aggregates (annual, latest available value)
WB_COUNTRIES = ["US", "EMU", "CN", "JP", "GB", "IN", "DE"]
WB_CPI = "FP.CPI.TOTL.ZG"      # Inflation, consumer prices (annual %)
WB_GDP = "NY.GDP.MKTP.KD.ZG"   # GDP growth (annual %)

# Policy rates, yield curve & breakeven inflation (FRED public CSV with
# automatic fallbacks to NY Fed, ECB Data Portal, and US Treasury CSVs)
FRED_POLICY = [
    ("DFF",    "US Fed Funds Rate"),
    ("ECBDFR", "ECB Deposit Rate"),
]
FRED_CURVE = ("T10Y2Y", "US 10Y − 2Y Treasury Spread", 4)   # (id, title, years shown)
FRED_BREAKEVEN = ("T10YIE", "US 10Y Breakeven Inflation")

# --- Cache lifetimes (seconds) ------------------------------------------------
TTL = {
    "market": 10 * 60,        # indices & commodities
    "fx": 60 * 60,            # FX reference rates
    "wb": 24 * 60 * 60,       # World Bank annual data
    "fred": 60 * 60,          # FRED / central-bank daily series
}

# Days of history used for sparklines / charts
SPARK_DAYS = 120
