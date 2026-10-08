# econ — Project Handoff

> **Convention:** this file is updated on *every* change session. Anyone (human or agent)
> should be able to resume work from this file alone. Newest entries go at the **top** of
> the Session Log (§9). Update §3 (Decisions), §6 (Status), §8 (Next steps) as things change.

---

## 1. Project goal

A **self-hosted, glanceable global macro dashboard** running on a Debian LXC container.
Target vibe: a simplified "world economy at a glance" wall-board — dark mode, big charts,
no day-trading noise. Core themes: global indices, commodities, currencies, inflation,
central-bank policy rates, and the US yield curve (recession indicator).

## 2. Environment

| Item | Value |
|---|---|
| Server | Debian LXC (Proxmox guest), hostname `econ` |
| Server IP | `192.168.0.149` (LAN only) |
| Access | root via SSH; web UI on LAN |
| Repo | `shashidaren/econ` (this repo) |
| Working branch | `arena/fb88ec58-econ` (session branch; merge to `main` via PR) |
| Deploy model | server does `git fetch --all && git reset --hard origin/main && ./install.sh` |
| App dir on server | `/opt/econ` |
| Port | **8080** (override: `ECON_PORT=xxxx ./install.sh`) |
| Service | `systemd` unit `econ-dashboard.service` |
| Runtime deps | **python3 + curl** — app is stdlib-pure (no pip, no venv, no Docker) |
| Disk cache | `/var/tmp/econ-dashboard-cache.json` (survives service restarts) |

## 3. Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Dashboard approach | **Custom dashboard app** (code in this repo) | Fully ours, glanceable wall-board, most repo-friendly |
| D2 | Install style | **Native** (apt python3 + curl + systemd) | Lightest footprint in LXC; zero pip/venv/Docker |
| D3 | Data sources | **No-key multi-source cascade** | Every panel has primary + independent fallback endpoints (Yahoo Finance → FRED → Stooq → CoinGecko for quotes; Frankfurter `.app`/`.dev` for FX; World Bank for macro; FRED `cosd=` → NY Fed / ECB Data Portal / US Treasury CSV for rates & curve) |
| D4 | HTTP transport | **`curl -4` (HTTP/2, IPv4) + IPv4-first `urllib`** | Solves LXC broken-IPv6 timeouts, Akamai HTTP/1.1 TLS tarpitting on `fred.stlouisfed.org`, Cloudflare blocks on `stooq.com`, and Yahoo 429s from bot User-Agents |
| D5 | Cache warming | **Fast macro first + 4-worker thread pool + disk persistence** | Prevents slow market-quote endpoints from blocking FX/World Bank/Rates on startup; restores last-good data on `systemctl restart` |
| D6 | Dead-upstream handling | **Per-provider circuit breakers** (30 min cooldown, 15 min after a 429) **+ parallel 3 s pre-flight probes** | A blocked host cost ~45 s *per card*; now the first probe trips the breaker and the other 14 symbols skip it in ~0 ms. Breaker state is printed in the board footer so an empty panel explains itself |
| D7 | Yahoo strategy | **One batched `v7/finance/spark` request per cycle** + 1.5 s pacing + `fc.yahoo.com` session cookie + stop at the first 429 | Yahoo 429s data-centre IPs that burst; 15 sequential chart calls reliably tripped it (Session 3 evidence) |
| D8 | FRED as a quote source | **Freshness-gated** (reject observations older than the series' `max_age_days`) + widen the `cosd=` window once on a 404 | FRED keeps discontinued series frozen (`DCOILWTI`/`DCOILBRENTEU` 404 on a 1-year window); a 2020 price must never render as today's WTI |
| D9 | Verification | **`tests/` (stdlib unittest, faked transport) + `tools/sim_lxc_network.py`** | The agent sandbox has no egress to these hosts, so the LXC's measured latencies are replayed against the real cascade code instead of guessed at |

Data-source rules: free, no signups; every source fails independently (panel shows
"awaiting data"/stale badge, board never breaks); polite fetch cadence via cache TTLs.

## 4. Architecture (v0.3)

```
dashboard/
  app.py        HTTP server (ThreadingHTTPServer) + disk-backed cache + 4-worker refresher
                + pre-flight step: warm_providers() then prefetch_yahoo() before the fan-out
  config.py     ALL panels/symbols/TTLs/FRED_QUOTES — edit this to change the board
  sources.py    multi-source fetchers + `curl -4` transport + per-provider circuit breakers
                + CLI diagnostics (`python3 sources.py` / `--doctor`)
  demo.py       same signatures as sources.py → synthetic data (ECON_DEMO=1)
  charts.py     server-side SVG: sparkline(), line_chart()
  render.py     single-page HTML/CSS builder (dark theme, source & stale badges,
                footer "source issues" + "upstream health" lines, auto-refresh)
tests/test_sources.py    22 stdlib unittests — cascade, breakers, FRED gate, rendering
tools/sim_lxc_network.py replays the LXC's measured latencies against the real cascade
tools/warm_ab.py         times warm_all() for any checkout (A/B comparisons)
systemd/econ-dashboard.service
install.sh      idempotent server bootstrap (apt python3 + curl → unit → enable → restart)
uninstall.sh
.env.example    optional overrides + reserved FRED key slot
```

- `/` board · `/api/summary` JSON (now includes `providers` breaker state) · `/healthz` probe
- Each refresh cycle: **pre-flight** (probe stooq hosts 3 s, one batched Yahoo spark request for
  every stale symbol) → 4-worker fan-out over fast macro sources first, then market quotes
- Disk cache (`/var/tmp/econ-dashboard-cache.json`) keeps last good data across `systemctl restart`
- Failed fetch keeps last good data and surfaces a ⚠ source-issue line in the footer
- Charts are inline SVG — **no CDN, works fully offline on LAN** once data is cached

## 5. Ops runbook (on the server)

```bash
# Deploy latest changes (overwrites any local ad-hoc edits in /opt/econ)
cd /opt/econ
git fetch --all
git reset --hard origin/main    # or origin/arena/fb88ec58-econ before PR merge
./install.sh

# Test upstream data sources directly from the LXC
python3 /opt/econ/dashboard/sources.py             # quick: one check per panel
python3 /opt/econ/dashboard/sources.py --doctor    # exhaustive: every provider, series
                                                   # and FRED candidate + breaker report

# Offline tests (no network needed; faked transport)
cd /opt/econ && python3 -m unittest discover -s tests -v

# Check API summary & service logs
curl -s localhost:8080/api/summary | python3 -c "import json,sys; s=json.load(sys.stdin); print('indices:',sum(q is not None for q in s['indices']),'/',len(s['indices']),'| commodities:',sum(q is not None for q in s['commodities']),'/',len(s['commodities']),'| fx:',len(s['fx']),'/',8,'| cpi:',len(s['cpi']),'| gdp:',len(s['gdp']),'| curve pts:',len(s['curve'])); print('errors:',s['errors']); print('providers:',[(p['provider'],p['state'],p['retry_in']) for p in s['providers']])"
journalctl -u econ-dashboard -n 30 --no-pager
```

Preview (no network needed): `ECON_DEMO=1 python3 dashboard/app.py` → sample data.

## 6. Current status

- [x] Repo + `handoff.md` convention
- [x] D1–D5 decided
- [x] App code (v0.1) — initial panels, demo mode, systemd unit (PR #1 merged to `main`)
- [x] First deploy on `192.168.0.149` executed; diagnosed live network behaviors:
  - `fx` (Frankfurter ECB) and `wb_cpi`/`wb_gdp` (World Bank) succeeded in <1s
  - `stooq.com` timed out; `yahoo` returned 429 on non-browser UA; `fred.stlouisfed.org`
    timed out when queried via Python `urllib` (HTTP/1.1 + full-history CSV without `cosd=`)
  - Sequential `warm_all()` starting with 15 market quotes delayed `fx`/`wb` by ~15 min on restart
- [x] **v0.2 multi-source + transport & concurrency upgrade committed to Git**:
  - `curl -4` (HTTP/2, IPv4, browser UA, URL-escaped tickers) + IPv4-first `urllib` fallback
  - Market quotes cascade: Yahoo (`query2`/`query1` with polite rate-lock) → FRED (`cosd=`) → Stooq (`.com`/`.pl`) → CoinGecko (`pax-gold` for Gold)
  - Rates & curve cascade: FRED (`cosd=` windowed CSV) → NY Fed EFFR JSON (`DFF`), ECB Data Portal CSV (`ECBDFR`), US Treasury Daily Par/Real Yield Curve CSV (`T10Y2Y`, `T10YIE`)
  - Refresher warms fast macro sources first using 4 worker threads + saves disk cache
  - Fixed card labels (`S&P 500` instead of raw symbol `^spx`) and footer error display
- [x] **v0.2 deployed on `192.168.0.149`** (2026-10-07). Verified live from the LXC:
  - `fx` 8/8 (1.06 s) · `wb_cpi`/`wb_gdp` 7/7 (3.47 s) · curve 1043 pts
  - FRED `fredgraph.csv` is **fast from the LXC** (DFF 0.11 s, ECBDFR 0.15 s, T10Y2Y 0.61 s,
    T10YIE 0.12 s) — the Session-2 tarpitting theory did not survive contact with the server
  - **Only 4/9 indices and 4/6 commodities filled.** Root causes, in order of impact:
    Yahoo Finance **HTTP 429** on every symbol after the first (~15 sequential chart calls),
    `stooq.com`/`stooq.pl` **TCP connect timeout after 5 s** (both, every symbol → ~45 s/card),
    FRED **HTTP 404** for `DCOILWTI` (discontinued series, empty `cosd=` window)
- [x] **v0.3 implemented and verified offline** — circuit breakers, pre-flight probes, batched
  Yahoo spark prefetch, FRED freshness gate, `--doctor`, 22 unit tests (see §9 Session 3)
- [ ] **Deploy v0.3 on `192.168.0.149`** and re-run the summary check + `sources.py --doctor`

## 7. Known risks / watch-list

- **Ad-hoc edits on `/opt/econ`**: During Session 1 post-merge debugging, heredoc patches were
  pasted directly into `/opt/econ/dashboard/{config,sources,app,render}.py`. Always use
  `git fetch --all && git reset --hard origin/main` (or the session branch) before `./install.sh`
  so git doesn't refuse to pull over local modifications.
- **Yahoo Finance 429s (the current headline problem)**: Yahoo serves the first request then
  rate-limits the IP. Mitigations in v0.3: one batched `v7/finance/spark` call per cycle instead
  of 15 chart calls, 1.5 s pacing (`ECON_YAHOO_PACE`), `fc.yahoo.com` session cookie
  (`ECON_YAHOO_COOKIE=0` to disable), stop-on-first-429, and a 15 min breaker. **Unverified from
  the LXC** — the spark endpoint and the cookie handshake both need one `--doctor` run.
- **5 indices depend solely on Yahoo**: `^dax`, `^ukx`, `^stx`, `^shc`, `^hsi` have no FRED
  equivalent. If the batched call still 429s, they stay empty. Options if `--doctor` says so:
  add CNBC's key-less `quote.cnbc.com` REST endpoint (history support unconfirmed), or trim the
  board to what FRED covers.
- **stooq is unreachable from the LXC** (`stooq.com`, `stooq.pl` and `http://stooq.com:80` all
  time out at TCP level). v0.3 makes that cost 3 s once per 30 min instead of 45 s per card. If
  `--doctor` confirms all three are dead from this network, delete them from `STOOQ_ENDPOINTS`.
- **FRED discontinued series**: `DCOILWTI` and `DCOILBRENTEU` return 404 for a 1-year window
  (last observations are from 2020). v0.3 widens the window once to confirm staleness and then
  refuses the data instead of showing a six-year-old price as today's. Needs a live replacement
  for WTI/Brent (see §8).
- **Silver has no verified fallback**: `xagusd` is Yahoo-only; `kinesis-silver` on CoinGecko is
  wired in as a guess and appears in `--doctor` — delete the `COINGECKO_MAP` line if it fails.
  `FRED_PROBE_CANDIDATES` lists series (`PSILVUSDM`, `PALUMUSDM`, …) to confirm at the same time.
- **FRED `fredgraph.csv` latency**: Mitigated by `&cosd=YYYY-MM-DD`, `curl -4` HTTP/2, and the
  NY Fed / ECB / US Treasury fallbacks. Session 3 evidence: FRED is actually fast from the LXC,
  so this risk is lower than believed in Session 2.
- **Sandbox has no egress to data hosts**: nothing upstream can be verified from the agent
  environment (only github/pypi are reachable). Every claim about live providers must come from a
  run on the LXC — `tests/` + `tools/sim_lxc_network.py` cover the logic in the meantime.

## 8. Next steps (backlog)

1. **Deploy v0.3 on the LXC** (merge this branch → `main`, then the §5 deploy block) and re-run
   the summary one-liner. Expected: same macro panels, faster warm-up, and a footer that names
   the provider that is failing instead of an unexplained empty card.
2. **Run `python3 /opt/econ/dashboard/sources.py --doctor` and paste the output back.** It answers
   every open question in one shot: does the batched Yahoo spark call work? does the cookie
   handshake return a cookie? which FRED series are alive (`SP500`, `NASDAQ100`, `DJIA`,
   `NIKKEI225`, `PCOPPUSDM`, `PWHEAMTUSDM`)? are `POILWTIUSDM`/`POILBREUSDM`/`PSILVUSDM` real
   replacements for the discontinued oil/silver series? is any stooq host reachable at all?
3. **Act on that output** (each is a 1–3 line change in `config.py`/`sources.py`):
   promote confirmed FRED series into `FRED_QUOTE_MAP`, delete `COINGECKO_MAP` entries that fail,
   drop dead hosts from `STOOQ_ENDPOINTS`.
4. If Yahoo still 429s even batched: evaluate CNBC's key-less `quote.cnbc.com` REST API for
   `^dax`, `^ukx`, `^stx`, `^shc`, `^hsi` (history support unconfirmed — probe first).
5. Ideas pool: global shipping (BDI) panel · debt-to-GDP panel · central-bank meeting
   calendar · multi-region yield curves · "snapshots" (PNG export for wall display).

## 9. Session log

### 2026-10-08 — Session 3: v0.2 live-deploy diagnosis → v0.3 (breakers, batched Yahoo, FRED gate)
- **Input:** the v0.2 deploy output pasted from `root@econ:/opt/econ`. Working: `fx` 8/8 (1.06 s),
  `wb_cpi`/`wb_gdp` 7/7 (3.47 s), `DFF`/`ECBDFR`/`T10Y2Y`/`T10YIE` all live off FRED CSV in
  0.11–0.61 s, curve 1043 pts. Broken: **indices 4/9, commodities 4/6**, and two quotes that took
  43.2 s / 55.4 s to resolve.
- **Root causes, from the pasted error strings (not guessed):**
  1. Yahoo **HTTP 429** on every symbol after the first (`^GDAXI`, `^FTSE`, `000001.SS`,
     `^STOXX50E`, `^HSI`, `CL=F`) — 15 sequential chart calls from one IP.
  2. `stooq.com` **and** `stooq.pl` TCP connect timeout after 5 s, retried for *every* symbol
     (~10 s/card/cycle wasted, and it is what pushed `cl.f` to 55 s).
  3. FRED **HTTP 404** for `DCOILWTI` → no oil fallback at all. (Session 2's "FRED tarpits from
     the LXC" theory was wrong: FRED answered in 0.11 s.)
- **Implemented v0.3** (`dashboard/sources.py`, `app.py`, `render.py`):
  - **Circuit breakers per provider** (`provider_available` / `note_provider_*`; 30 min cooldown,
    15 min after a 429, instant on TCP failure). `curl` now reports the HTTP status via a `-w`
    marker so a 429 is distinguishable from a timeout. State is exposed as `providers` in
    `/api/summary` and rendered as a footer "upstream health" line.
  - **Pre-flight** `warm_providers()`: parallel 3 s probes of all `STOOQ_ENDPOINTS` (shared list,
    so fetcher and probe cannot drift), plus `--connect-timeout 3` for stooq.
  - **Yahoo**: `prefetch_yahoo()` pulls every stale symbol in **one** `v7/finance/spark` request
    per cycle; pacing 0.45 s → 1.5 s (`ECON_YAHOO_PACE`); `fc.yahoo.com` cookie handshake
    (`ECON_YAHOO_COOKIE=0` disables); stop at the first 429 instead of hammering host 2.
  - **FRED**: `_fred_quote_history` refuses observations older than the series' `max_age_days`;
    `_fred_csv_series(window_years=…)` widens the window once on a 404, so the error becomes
    "last observation 2020-04-24 is 2357d old" instead of a bare 404. FRED is now breaker-tracked
    so a tarpitting FRED costs ~0 ms instead of 12 s × 13 series.
  - **Misc**: `COINGECKO_MAP` (adds a silver fallback, unverified), `_ustreasury_*` now surface the
    underlying cause, `sources.py --doctor` (6-section probe incl. FRED candidates).
- **Verification — all offline; the sandbox has no egress to any data host:**
  - `python3 -m unittest discover -s tests` → **22 passed** (cascade order, breakers, FRED gate,
    404 widening, stooq cooldown, curl status parsing, footer rendering, `warm_all` pre-flight).
  - Demo mode on `:8097` → `/healthz` 200; `/api/summary` 200 with 9/9 indices, 6/6 commodities,
    8/8 fx, 7 cpi, 7 gdp, 1460 curve pts, 365 breakeven pts, `providers: []`; `/` 200, 81.7 kB,
    26 inline SVG charts.
  - Live mode with every host blocked → all three endpoints still 200, 6 error lines, breaker
    footer rendered, no crash.
  - `tools/sim_lxc_network.py` replaying the LXC's measured latencies against the real cascade:
    **`warm_all()` 38.4 s → 8.6 s, keys filled 14/22 → 16/22** (indices 4/9 → 5/9,
    commodities 3/6 → 4/6) on the identical fake network. The old code reproduced the deploy
    numbers (4/9 indices, fx 8/8, cpi/gdp 7/7) before the fix, which is what makes the sim credible.
- **Still open:** none of the new Yahoo-spark / cookie / FRED-candidate behaviour is confirmed
  against the real network. One `python3 /opt/econ/dashboard/sources.py --doctor` run on the LXC
  settles all of it (§8.2).

### 2026-10-07 — Session 2: live-deploy root-cause fixes + v0.2 pushed to Git
- Reviewed terminal logs from the first live run on `root@econ:/opt/econ`:
  1. `fx` (8/8), `wb_cpi` (7/7), and `wb_gdp` (7/7) succeeded in ~1s once the refresher
     reached them, but were starved behind 15 sequential market-quote timeouts on restart.
  2. `stooq.com` timed out, `query1.finance.yahoo.com` returned HTTP 429 (bot User-Agent /
     bursting), and `fred.stlouisfed.org` timed out in `ssl.read` after 15s (Akamai tarpitting
     Python's HTTP/1.1 `urllib` + unwindowed `fredgraph.csv` generating full history since 1954).
  3. The ad-hoc shell patches from the end of Session 1 (Yahoo map + Plan B FRED quotes) had
     been pasted on the server `/opt/econ` only and were not yet in Git.
- **Implemented & committed v0.2 in Git (`dashboard/config.py`, `sources.py`, `app.py`, `render.py`, `demo.py`, `install.sh`):**
  - **Transport:** `_get()` now uses `curl -4 -g -fsSL --compressed` (HTTP/2, IPv4-forced,
    browser UA, 5s connect timeout) with an IPv4-first `urllib` fallback.
  - **Market quotes cascade:** Yahoo Finance (`query2`/`query1` v8 chart with URL-encoded
    tickers and 0.45s rate lock) → FRED (`SP500`, `NASDAQ100`, `DJIA`, `NIKKEI225`,
    `DCOILWTI`, `DCOILBRENTEU`, `PCOPPUSDM`, `PWHEAMTUSDM`) → Stooq (`.com`/`.pl`) →
    CoinGecko (`pax-gold` for `xauusd`). Added `fred_quote()` helper and `FRED_QUOTES` in `config.py`.
  - **Policy rates & yield curve cascade:** Added `&cosd=YYYY-MM-DD` to all `fredgraph.csv`
    requests AND added automatic official fallbacks: NY Fed Markets API (`DFF`), ECB Data
    Portal API (`ECBDFR`), and US Treasury Daily Par/Real Yield Curve CSVs (`T10Y2Y`, `T10YIE`).
  - **Refresher & cache:** Reordered registry so fast macro sources warm first, parallelized
    warming across 4 worker threads, and added atomic disk cache persistence
    (`/var/tmp/econ-dashboard-cache.json`) so `systemctl restart` keeps last good data.
  - **UI fixes:** Fixed market card titles to display human names (`S&P 500`) instead of raw
    tickers (`^spx`), added dynamic source & `stale` badges, and wired `errors` into the footer.
  - Added CLI self-test (`python3 /opt/econ/dashboard/sources.py`).

### 2026-10-07 — Session 1: kickoff + v0.1 built
- Inspected repo: empty (`README.md` only) at commit `8e74b84`.
- Server confirmed: Debian LXC `econ` at `192.168.0.149`, root access.
- Established this `handoff.md` convention.
- **Decisions locked (D1–D3):** custom app, native install, no-key sources.
- **Built v0.1 of the dashboard:** stdlib-only Python app (see §4), systemd unit,
  idempotent `install.sh`/`uninstall.sh`, demo mode for offline previews.
- Tested in sandbox: all 7 sections render, `/api/summary` OK, graceful per-source
  error handling in place. 27 inline SVG charts, dark wall-board UI, 5-min auto-refresh.
- Merged PR #1 (`arena/4b243469-econ` → `main` at `b594350`).
