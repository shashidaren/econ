# econ

Self-hosted, glanceable **global macro dashboard** for a Debian LXC container
(hostname `econ`, LAN IP `192.168.0.149`, port `8080`).

A dark-mode wall-board for world indices, commodities, currencies, inflation,
central-bank rates, and the US yield curve — plus a separate **Stocks** tab that
screens a fixed US watchlist and lists every short-term technical candidate
under $50 that passes (up to seven, ranked) with backward-looking
low/medium/high risk labels. Served
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

## Automatic updates (cron)

`install.sh` also drops a cron entry, so merged work reaches the LXC hands-free:

| Item | Default |
|---|---|
| Cron file | `/etc/cron.d/econ-update` (written by `install.sh`, safe to edit) |
| Schedule | every 30 min — override with `ECON_UPDATE_SCHEDULE='0 * * * *' ./install.sh` |
| Script | `/opt/econ/tools/auto_update.sh` (tracks `origin/main`) |
| Log | `/var/log/econ-update.log` — `tail -f /var/log/econ-update.log` |

```bash
sudo /opt/econ/tools/auto_update.sh                    # run it now
ECON_UPDATE_DRY_RUN=1 sudo /opt/econ/tools/auto_update.sh   # report only, change nothing
sudo rm -f /etc/cron.d/econ-update                     # stop unattended updates
```

How it behaves:

- **Only `main` is tracked.** Anything merged to `main` goes live on the next run
  (up to 30 min later); feature branches and open PRs are ignored until merged.
- **It restarts the service only when the commit actually changed**, so an idle
  run never disturbs the dashboard.
- **It runs `git reset --hard origin/main`, exactly like the manual deploy above,
  so every hand edit inside `/opt/econ` is discarded.** Edit in git, never on
  the server — the next run will overwrite it.
- Runs are serialised with `flock`; an overlapping run skips instead of queueing.
- If the checkout is on some other branch, the job refuses rather than merging
  `main` into it.
- The only gate before restart is `install.sh`'s import smoke test. A commit that
  breaks the Python imports therefore leaves the **running** service on the old
  code and logs an error. There is no test-suite gate and no automatic rollback —
  if you want one, add it in `tools/auto_update.sh`.

Preview without a server (sample data): `ECON_DEMO=1 python3 dashboard/app.py`

## Stocks watchlist

- `/stocks` — every passing name from the editable universe in
  `dashboard/config.py`, ranked, up to **seven** (override with
  `ECON_STOCK_MAX_RESULTS=10`, hard-capped at 32); `/api/stocks` returns the
  same screen as JSON and `/api/summary` includes it as `stock_screen`.
- US-listed names only; last daily close must be **$5 to under $50**, average
  20-session dollar volume at least **$5 million**, quote no more than five
  calendar days old, positive 20-session return, and price above its 20-day
  average. The screen ranks 5/20/60-session momentum after small volatility and
  drawdown penalties. Every passer is listed; the page shows the eligible count
  even when the display is capped.
- **Update cadence:** the screen re-checks its providers at most once an hour
  (`TTL["stocks"]`) on the dashboard's 5-minute cycle, and the page shows when
  the next check is due. The underlying data is **daily closes**, so the list
  itself only changes after the U.S. close (~04:00–05:00 Malaysia time) — it is
  not intraday, and an unchanged list between closes is expected.
- Risk labels use 20-session annualized realized volatility and 60-session
  drawdown only. They are backward-looking, not a measure of total or future
  risk. The screen has no fundamentals, news, earnings calendar, analyst
  targets, intraday prices, or trade execution. A share price under $50 does not
  mean the stock is cheap; this is a research shortlist, not a buy signal.
- History sources: Tencent adjusted daily K-lines (primary), one batched Yahoo
  Finance spark request (fallback), then Stooq. The page labels Kuala Lumpur and
  U.S. Eastern timestamps, identifies daily-bar dates as U.S. market session
  dates, and shows incomplete history/provider errors rather than concealing
  them. If all providers fail, it shows unavailable/no candidates rather than
  guessed or synthetic live picks. DEMO mode is synthetic and must not be used
  for investment decisions.
- For Moomoo Malaysia users, the 32-symbol universe remains editable but is not
  validated against Moomoo's live symbol/trading-eligibility list. Confirm the
  ticker in the app. The optional MYR amount uses the dashboard's fresh ECB
  reference rate via Frankfurter, is not an executable conversion or all-in
  price, and excludes applicable fees, stamp duty, spreads, and FX conversion
  charges. See the [Moomoo Malaysia fractional-share guide](https://www.moomoo.com/my/support/topic9_193)
  and [current U.S. fee schedule](https://www.moomoo.com/my/support/topic9_136).

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
