# econ

Self-hosted, glanceable **global macro dashboard** for a Debian LXC container
(hostname `econ`, LAN IP `192.168.0.149`, port `8080`).

A single dark-mode wall-board: world indices, commodities, currencies, inflation,
central-bank rates, and the US yield curve — served by a **Python-stdlib-only app**
(no pip, no venv, no Docker, no CDN, no API keys).

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
| World indices & commodities | Yahoo Finance — one batched `v7/finance/spark` request per cycle, `v8` chart per symbol as a retry | FRED public CSV, freshness-gated (`SP500`, `NASDAQ100`, `DJIA`, `NIKKEI225`, `DCOILWTI`, `DCOILBRENTEU`, `PCOPPUSDM`, `PWHEAMTUSDM`) → Stooq (`.com`/`.pl`, https+http) → CoinGecko tokenised metal (`pax-gold`, `kinesis-silver`) |
| Currencies (FX, USD base) | Frankfurter (`api.frankfurter.app`, ECB rates) | `api.frankfurter.dev/v1` |
| Annual inflation & GDP growth | World Bank API (`api.worldbank.org`) | Cached last-good snapshot on disk |
| Central-bank policy rates (`DFF`, `ECBDFR`) | FRED public CSV (`fredgraph.csv?cosd=...`) | NY Fed Markets API (`markets.newyorkfed.org`) for `DFF`; ECB Data Portal (`data-api.ecb.europa.eu`) for `ECBDFR` |
| US 10Y–2Y yield curve & 10Y breakeven | FRED public CSV (`T10Y2Y`, `T10YIE`) | Official US Treasury Daily Par & Real Yield Curve CSV (`home.treasury.gov`) |

### Resilience

Every upstream sits behind a **circuit breaker**: a host that 429s or times out is skipped for
30 minutes (15 after a rate limit) instead of being retried by all 15 quote fetchers, and a 3-second
pre-flight probe catches dead hosts before the fan-out starts. Slow providers therefore cost
milliseconds rather than ~45 s per card, and the board's footer names the provider that is failing
instead of leaving an empty card unexplained.

`/api/summary` includes the breaker state (`providers`), so the board is debuggable with `curl`.

Not investment advice — it's a glance-board, not a trading terminal.
