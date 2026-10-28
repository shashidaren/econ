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
# automatically across Yahoo Finance -> FRED -> CNBC -> Tencent/Sina -> Stooq).
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

# Commodities — the pulse of the real economy. Most use Yahoo -> FRED ->
# Stooq (and CoinGecko for gold/silver); aluminum and nickel are FRED-only
# because no Yahoo/Stooq ticker has been verified for these new cards.
COMMODITIES = [
    ("WTI Crude",   "cl.f"),
    ("Brent Crude", "cb.f"),
    ("Gold",        "xauusd"),
    ("Silver",      "xagusd"),
    ("Copper",      "hg.f"),
    ("Wheat",       "zw.f"),
    ("Aluminium",   "al.f"),
    ("Nickel",      "ni.f"),
]

# FRED quote series (label, FRED series id, max_age_days before card shows a
# "stale" badge). Brent's monthly series is a fallback to the daily quote.
# IMF monthly commodities are allowed 120 days for their publication lag.
FRED_QUOTES = [
    ("S&P 500",                         "SP500",        10),
    ("NASDAQ 100",                      "NASDAQ100",    10),
    ("Dow Jones 30",                    "DJIA",         10),
    ("Nikkei 225 (JP)",                 "NIKKEI225",    10),
    ("WTI Crude — global (monthly)",    "POILWTIUSDM", 120),
    ("Brent Crude",                     "DCOILBRENTEU", 10),
    ("Brent Crude — monthly fallback",  "POILBREUSDM",  120),
    ("Copper — global (monthly)",       "PCOPPUSDM",    120),
    ("Wheat — global (monthly)",        "PWHEAMTUSDM",  120),
    ("Aluminium — global (monthly)",    "PALUMUSDM",    120),
    ("Nickel — global (monthly)",       "PNICKUSDM",    120),
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
WB_COUNTRIES = ["US", "EMU", "CN", "JP", "GB", "IN", "DE", "MY"]
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
FRED_REAL = ("DFII10", "US 10Y Real Yield")  # TIPS yield; the usual gold headwind/tailwind

# --- Economy 101 tab (US vs Malaysia) ---------------------------------------
# Annual World Bank series for the two economies, drawn from the same provider
# as the macro panel above. No new upstream services: the FX (ECB/Frankfurter)
# and Fed-funds (FRED) numbers this tab uses are already fetched for the board.
ECON_COUNTRIES = [
    {"code": "US", "name": "United States", "currency": "USD",
     "central_bank": "Federal Reserve", "rate_label": "Fed funds rate"},
    {"code": "MY", "name": "Malaysia", "currency": "MYR",
     "central_bank": "Bank Negara Malaysia",
     "rate_label": "Overnight Policy Rate (OPR)"},
]

# Year span shown on the tab's charts (12 annual points is plenty for a glance).
ECON_HISTORY_YEARS = int(os.environ.get("ECON_HISTORY_YEARS", "12"))

# Indicator keys are stable ids used by economy.py; `explain` is layman wording.
ECON_INDICATORS = [
    {"key": "gdp_growth", "indicator": "NY.GDP.MKTP.KD.ZG",
     "label": "Economic growth", "short": "Growth", "unit": "%",
     "explain": "How much more the country produced than a year earlier, "
                "after stripping out price changes."},
    {"key": "inflation", "indicator": "FP.CPI.TOTL.ZG",
     "label": "Inflation", "short": "Inflation", "unit": "%",
     "explain": "How much the average basket of consumer prices rose versus "
                "a year earlier."},
    {"key": "unemployment", "indicator": "SL.UEM.TOTL.ZS",
     "label": "Unemployment", "short": "Unemployment", "unit": "%",
     "explain": "Share of people who want work but do not have it "
                "(ILO modelled estimate)."},
    {"key": "trade", "indicator": "NE.TRD.GNFS.ZS",
     "label": "Trade openness", "short": "Trade", "unit": "% of GDP",
     "explain": "Exports plus imports as a share of the whole economy — how "
                "much the outside world matters to it."},
]

# Malaysia's OPR has no stable key-less JSON endpoint, so it is a manual
# reference value, clearly labelled as such wherever it is displayed. Update
# it whenever Bank Negara Malaysia changes the rate (bnm.gov.my).
ECON_MY_OPR_AS_OF = os.environ.get("ECON_MY_OPR_AS_OF", "2023-05-03")
try:
    ECON_MY_OPR = float(os.environ.get("ECON_MY_OPR", "3.00"))
except ValueError:
    ECON_MY_OPR = 3.00

# --- Cache lifetimes (seconds) ------------------------------------------------
TTL = {
    "market": 10 * 60,        # indices & commodities
    "fx": 60 * 60,            # FX reference rates
    "wb": 24 * 60 * 60,       # World Bank annual data
    "fred": 60 * 60,          # FRED / central-bank daily series
    "economy": 24 * 60 * 60,  # Economy 101 annual World Bank series
}

# Days of history used for sparklines / charts
SPARK_DAYS = 120

# --- Short-term stock watchlist ---------------------------------------------
# Editable US-listed watch universe. Tencent codes include the exchange suffix
# used by its K-line endpoint (.OQ = Nasdaq, .N = NYSE). A live price filter is
# applied later; the list is not a statement that any of these stocks is a buy.
STOCK_UNIVERSE = [
    ("PFE",   "Pfizer",                    "Health Care",          "usPFE.N"),
    ("BMY",   "Bristol Myers Squibb",       "Health Care",          "usBMY.N"),
    ("CVS",   "CVS Health",                 "Health Care",          "usCVS.N"),
    ("VTRS",  "Viatris",                    "Health Care",          "usVTRS.OQ"),
    ("BAC",   "Bank of America",            "Financials",           "usBAC.N"),
    ("C",     "Citigroup",                  "Financials",           "usC.N"),
    ("SOFI",  "SoFi Technologies",           "Financials",           "usSOFI.OQ"),
    ("NU",    "Nu Holdings",                "Financials",           "usNU.N"),
    ("PYPL",  "PayPal",                     "Financials",           "usPYPL.OQ"),
    ("T",     "AT&T",                       "Communication Services", "usT.N"),
    ("VZ",    "Verizon",                    "Communication Services", "usVZ.N"),
    ("CMCSA", "Comcast",                    "Communication Services", "usCMCSA.OQ"),
    ("WBD",   "Warner Bros. Discovery",       "Communication Services", "usWBD.OQ"),
    ("INTC",  "Intel",                      "Information Technology", "usINTC.OQ"),
    ("HPQ",   "HP Inc.",                    "Information Technology", "usHPQ.N"),
    ("HPE",   "Hewlett Packard Enterprise",  "Information Technology", "usHPE.N"),
    ("NOK",   "Nokia ADR",                  "Information Technology", "usNOK.N"),
    ("SNAP",  "Snap",                       "Communication Services", "usSNAP.N"),
    ("F",     "Ford Motor",                 "Consumer Discretionary", "usF.N"),
    ("GM",    "General Motors",              "Consumer Discretionary", "usGM.N"),
    ("CCL",   "Carnival",                   "Consumer Discretionary", "usCCL.N"),
    ("NCLH",  "Norwegian Cruise Line",        "Consumer Discretionary", "usNCLH.N"),
    ("LYFT",  "Lyft",                       "Consumer Discretionary", "usLYFT.OQ"),
    ("AAL",   "American Airlines Group",     "Industrials",          "usAAL.OQ"),
    ("KHC",   "Kraft Heinz",                "Consumer Staples",     "usKHC.OQ"),
    ("CAG",   "Conagra Brands",              "Consumer Staples",     "usCAG.N"),
    ("OXY",   "Occidental Petroleum",        "Energy",               "usOXY.N"),
    ("DVN",   "Devon Energy",               "Energy",               "usDVN.N"),
    ("CTRA",  "Coterra Energy",             "Energy",               "usCTRA.N"),
    ("AA",    "Alcoa",                      "Materials",            "usAA.N"),
    ("PLUG",  "Plug Power",                 "Industrials",          "usPLUG.OQ"),
    ("CHPT",  "ChargePoint",                "Industrials",          "usCHPT.N"),
]
STOCK_HISTORY_SESSIONS = 130
STOCK_MIN_PRICE = float(os.environ.get("ECON_STOCK_MIN_PRICE", "5"))
STOCK_MAX_PRICE = float(os.environ.get("ECON_STOCK_MAX_PRICE", "50"))
STOCK_MIN_AVG_DOLLAR_VOLUME = float(
    os.environ.get("ECON_STOCK_MIN_DOLLAR_VOLUME", "5000000"))
# Show every ticker that passes the screen, up to this many (7 by default).
# Override with ECON_STOCK_MAX_RESULTS=10; the screener clamps it at 32.
STOCK_MAX_RESULTS = int(os.environ.get("ECON_STOCK_MAX_RESULTS", "7"))

# Cache daily-history screens for an hour; historical closes do not justify
# hammering keyless providers every five-minute dashboard refresh.
TTL["stocks"] = 60 * 60

# --- Delayed / intraday stock quotes (Stocks tab price overlay) --------------
# Keyless ~15-min-delayed quotes (Sina hq.sinajs.cn -> Nasdaq quote info ->
# Tencent qt.gtimg.cn -> Yahoo chart) refreshed every 5 min on the board cycle
# for the *displayed* picks only. Overlay only: never alters filters or rank.
STOCK_QUOTES_ENABLED = os.environ.get("ECON_STOCK_QUOTES", "1") not in ("0", "false", "no")
try:
    STOCK_QUOTE_TTL = max(60, int(os.environ.get("ECON_STOCK_QUOTE_TTL", "300")))
except ValueError:
    STOCK_QUOTE_TTL = 300
TTL["stock_quotes"] = STOCK_QUOTE_TTL

# --- Stock headlines (Stocks tab) --------------------------------------------
# Up to a few recent headlines per *displayed* ticker, from key-less public RSS
# (Google News -> Bing News -> Yahoo Finance headline feed). Headlines are
# context only: they never change which tickers pass or how they are ranked.
STOCK_NEWS_ENABLED = os.environ.get("ECON_STOCK_NEWS", "1") not in ("0", "false", "no")
try:
    STOCK_NEWS_WINDOW_DAYS = max(1, min(int(os.environ.get("ECON_STOCK_NEWS_DAYS", "7")), 30))
except ValueError:
    STOCK_NEWS_WINDOW_DAYS = 7
try:
    STOCK_NEWS_PER_SYMBOL = max(1, min(int(os.environ.get("ECON_STOCK_NEWS_PER_SYMBOL", "3")), 5))
except ValueError:
    STOCK_NEWS_PER_SYMBOL = 3
