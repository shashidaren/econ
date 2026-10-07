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

# --- Panels -----------------------------------------------------------------

# Global equity indices  (Stooq symbols)
INDICES = [
    ("S&P 500",           "^spx"),
    ("Nasdaq 100",        "^ndx"),
    ("Dow Jones 30",      "^dji"),
    ("Euro Stoxx 50",     "^stx"),
    ("DAX (Germany)",     "^dax"),
    ("FTSE 100 (UK)",     "^ukx"),
    ("Nikkei 225 (JP)",   "^nkx"),
    ("Shanghai Composite","^shc"),
    ("Hang Seng (HK)",    "^hsi"),
]

# Commodities — the pulse of the real economy  (Stooq symbols)
COMMODITIES = [
    ("WTI Crude",  "cl.f"),
    ("Brent Crude","cb.f"),
    ("Gold",       "xauusd"),
    ("Silver",     "xagusd"),
    ("Copper",     "hg.f"),
    ("Wheat",      "zw.f"),
]

# Currencies, USD base, via ECB reference rates (frankfurter.app)
FX_CURRENCIES = [
    ("Euro",             "EUR"),
    ("Japanese Yen",     "JPY"),
    ("Chinese Yuan",     "CNY"),
    ("British Pound",    "GBP"),
    ("Australian Dollar","AUD"),
    ("Swiss Franc",      "CHF"),
    ("Indian Rupee",     "INR"),
    ("Malaysian Ringgit","MYR"),
]

# World Bank macro aggregates (annual, latest available value)
WB_COUNTRIES = ["US", "EMU", "CN", "JP", "GB", "IN", "DE"]
WB_CPI = "FP.CPI.TOTL.ZG"      # Inflation, consumer prices (annual %)
WB_GDP = "NY.GDP.MKTP.KD.ZG"   # GDP growth (annual %)

# US Treasury / Fed series via FRED's public fredgraph.csv endpoint (no API key)
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
    "fred": 60 * 60,          # FRED daily series
}

# Days of history used for sparklines / charts
SPARK_DAYS = 120
