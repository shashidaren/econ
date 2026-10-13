# econ

Self-hosted, glanceable **global macro dashboard** for a Debian LXC container
(hostname `econ`, LAN IP `192.168.0.149`, port `8080`).

A dark-mode wall-board for world indices, commodities, currencies, inflation,
central-bank rates, and the US yield curve — plus a separate **Stocks** tab that
screens a fixed US watchlist and shows at most three short-term technical
candidates under $50 with backward-looking low/medium/high risk labels. Served
by a **Python-stdlib-only app** (no pip, no venv, no Docker, no CDN, no API keys).

**Start here → [`handoff.md`](handoff.md)** — project state, decisions, ops runbook,
and the session log. It is updated on every change session.

## Quick start (on the server)

```bash
apt install -y git curl
git clone https://github.com/shashidaren/econ /opt/econ
cd /opt/econ && ./install.sh
# → http://192.168.0.149:8080
```

Deploy updates any time (resets any local ad-hoc edits on the server):

```bash
cd /opt/econ && git fetch --all && git reset --hard origin/main && ./install.sh
```

Preview without a server (sample data): `ECON_DEMO=1 python3 dashboard/app.py`

## Stocks watchlist

- `/stocks` — top three technical candidates from the editable universe in
  `dashboard/config.py`; `/api/stocks` returns the same screen as JSON and
  `/api/summary` includes it as `stock_screen`.
- US-listed names only; last daily close must be **$5 to under $50**, average
  20-session dollar volume at least **$5 million**, quote no more than five
  calendar days old, positive 20-session return, and price above its 20-day
  average. The screen ranks 5/20/60-session momentum after small volatility and
  drawdown penalties. Results refresh hourly.
- Risk labels use 20-session annualized realized volatility and 60-session
  drawdown only. They are backward-looking, not a measure of total or future
  risk. The screen has no fundamentals, news, earnings calendar, analyst
  targets, intraday prices, or trade execution. A share price under $50 does not
  mean the stock is cheap; this is a research shortlist, not a buy signal.
- History sources: Tencent adjusted daily K-lines (primary), one batched Yahoo
  Finance spark request (fallback), then Stooq. Tencent's US-stock K-line path
  and exchange suffixes still need live verification on the target LXC; if all
  providers fail, the screen shows unavailable/no candidates rather than guessed
  or synthetic live picks. DEMO mode is synthetic and must not be used for
  investment decisions.

Test live upstream sources directly on the server:

```bash
python3 /opt/econ/dashboard/sources.py            # one check per panel
python3 /opt/econ/dashboard/sources.py --doctor   # every provider + FRED series, with timings
```

Run the offline test suite (no network, faked transport):

```bash
python3 -m unittest discover -s tests -v
```

## Data sources & automatic fallbacks (all free, no keys)

| Panel | Primary | Automatic Fallback(s) |
|---|---|---|
| World indices & commodities | Yahoo Finance — current warm path tries one batched `v7/finance/spark` request first, then v8 chart per unserved symbol; `--doctor` probes v8 charts with the cookie before spark | FRED public CSV, freshness-gated (`SP500`, `NASDAQ100`, `DJIA`, `NIKKEI225`, monthly `POILWTIUSDM`, daily `DCOILBRENTEU` → monthly `POILBREUSDM`, `PCOPPUSDM`, `PWHEAMTUSDM`, `PALUMUSDM`, `PNICKUSDM`; 120-day freshness for monthly IMF data) → Stooq (`.com`/`.pl`, https+http) → CoinGecko tokenised metals (`pax-gold`, `kinesis-silver`) |
| Currencies (FX, USD base) | Frankfurter (`api.frankfurter.app`, ECB rates) | `api.frankfurter.dev/v1` |
| Annual inflation & GDP growth | World Bank API (`api.worldbank.org`) | Cached last-good snapshot on disk |
| Central-bank policy rates (`DFF`, `ECBDFR`) | FRED public CSV (`fredgraph.csv?cosd=...`) | NY Fed Markets API (`markets.newyorkfed.org`) for `DFF`; ECB Data Portal (`data-api.ecb.europa.eu`) for `ECBDFR` |
| US 10Y–2Y yield curve & 10Y breakeven | FRED public CSV (`T10Y2Y`, `T10YIE`) | Official US Treasury Daily Par & Real Yield Curve CSV (`home.treasury.gov`) |
| Short-term US stock history | Tencent daily K-lines (per configured watchlist symbol) | One Yahoo Finance `v7/finance/spark` batch → Stooq `.us` daily CSV |

### Resilience

Every upstream sits behind a **circuit breaker**: a host that 429s or times out is skipped for
30 minutes (15 after a rate limit) instead of being retried by all quote fetchers, and a 3-second
pre-flight probe catches dead hosts before the fan-out starts. The HTTP transport does not retry
TCP failures with a second client; urllib fallback is reserved for TLS/HTTP-protocol errors and
shares curl's remaining time budget. Confirmed FRED 404s are negatively cached for 24 hours, and
the one wider FRED probe is limited to five years. Slow providers therefore cost milliseconds
rather than repeated multi-second retries, and the board's footer names the provider that is
failing instead of leaving an empty card unexplained.

`/api/summary` includes the breaker state (`providers`), so the board is debuggable with `curl`.

Not investment advice — it's a glance-board, not a trading terminal.
