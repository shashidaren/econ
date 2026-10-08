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
| Working branch | `arena/8d320fd8-econ` (this session; PR #3 merged to `main` on 2026-10-08; this follow-up goes through a new PR) |
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
| D3 | Data sources | **No-key multi-source cascade** | Every panel has primary + independent fallback endpoints (Yahoo Finance → FRED → Stooq → CoinGecko for supported quotes; aluminum/nickel are FRED-only; Frankfurter `.app`/`.dev` for FX; World Bank for macro; FRED `cosd=` → NY Fed / ECB Data Portal / US Treasury CSV for rates & curve) |
| D4 | HTTP transport | **`curl -4` (HTTP/2, IPv4) + bounded IPv4-first `urllib` fallback only for TLS/HTTP-protocol errors** | TCP failures and completed HTTP status errors are not retried with a second client; fallback shares the remaining total timeout, avoiding Stooq's measured 21 s curl-plus-urllib penalty |
| D5 | Cache warming | **Fast macro first + 4-worker thread pool + disk persistence** | Prevents slow market-quote endpoints from blocking FX/World Bank/Rates on startup; restores last-good data on `systemctl restart` |
| D6 | Dead-upstream handling | **Per-provider circuit breakers** (30 min cooldown, 15 min after a 429) **+ parallel 3 s pre-flight probes** | A blocked host cost ~45 s *per card*; now the first probe trips the breaker and the other 14 symbols skip it in ~0 ms. Breaker state is printed in the board footer so an empty panel explains itself |
| D7 | Yahoo strategy | **Current pipeline: one batched `v7/finance/spark` request per cycle**, paced at 1.5 s with the `fc.yahoo.com` cookie; `--doctor` now probes v8 charts with that cookie before spark and resets Yahoo breakers between tests | The LXC measured spark HTTP 429 (0.30 s) but did not actually test v8; if the owner confirms v8 works, prefer chart+cookie and make spark optional (pending live evidence) |
| D8 | FRED as a quote source | **Freshness-gated** + one 404 widen to at most 5 years (5 s timeout) + process-local negative cache for confirmed dead IDs (24 h TTL) | Confirmed series replace dead WTI/silver/gold IDs; stale observations must never render as current prices, and dead IDs must not be paid for every refresh |
| D9 | Verification | **`tests/` (stdlib unittest, faked transport) + `tools/sim_lxc_network.py`** | The agent sandbox has no egress to these hosts, so the LXC's measured latencies are replayed against the real cascade code instead of guessed at |
| D10 | Monthly commodity series | **Use the IMF monthly WTI / Brent fallback, copper, wheat, aluminum, and nickel with a 120-day freshness limit; daily Brent remains primary** | The LXC measured the newest confirmed monthly observations at 99 days old; the former 55-day gate incorrectly marked them stale |

Data-source rules: free, no signups; every source fails independently (panel shows
"awaiting data"/stale badge, board never breaks); polite fetch cadence via cache TTLs.

## 4. Architecture (v0.3 deployed; v0.4 follow-up pending owner merge)

```
dashboard/
  app.py        HTTP server (ThreadingHTTPServer) + disk-backed cache + 4-worker refresher
                + pre-flight step: warm_providers() then prefetch_yahoo() before the fan-out
  config.py     ALL panels/symbols/TTLs/FRED_QUOTES — edit this to change the board
  sources.py    multi-source fetchers + bounded curl/urllib transport + provider breakers
                + 24 h FRED 404 cache, 5 y retry limit, Treasury header normalization,
                + CLI diagnostics (`python3 sources.py` / `--doctor`)
  demo.py       same signatures as sources.py → synthetic data (ECON_DEMO=1)
  charts.py     server-side SVG: sparkline(), line_chart()
  render.py     single-page HTML/CSS builder (dark theme, source & stale badges,
                footer "source issues" + "upstream health" lines, auto-refresh)
tests/test_sources.py    37 stdlib unittests — cascade, breakers, FRED gate/cache, transport,
                         Yahoo doctor isolation, Treasury headers, rendering
tools/sim_lxc_network.py replays the LXC's measured latencies against the real cascade
tools/warm_ab.py         times warm_all() for any checkout (A/B comparisons)
systemd/econ-dashboard.service
install.sh      idempotent server bootstrap (apt python3 + curl → unit → enable → restart)
uninstall.sh
.env.example    optional overrides + reserved FRED key slot
```

- `/` board · `/api/summary` JSON (now includes `providers` breaker state) · `/healthz` probe
- Each refresh cycle: **pre-flight** (probe stooq hosts 3 s, one batched Yahoo spark request for
  stale Yahoo-mapped symbols) → 4-worker fan-out over fast macro sources first, then market quotes
- Disk cache (`/var/tmp/econ-dashboard-cache.json`) keeps last good data across `systemctl restart`
- Failed fetch keeps last good data and surfaces a ⚠ source-issue line in the footer
- Charts are inline SVG — **no CDN, works fully offline on LAN** once data is cached

## 5. Ops runbook (on the server)

```bash
# Deploy latest changes (overwrites any local ad-hoc edits in /opt/econ)
cd /opt/econ
git fetch --all
git reset --hard origin/main    # run after the owner merges the follow-up PR
./install.sh

# Test upstream data sources directly from the LXC
python3 /opt/econ/dashboard/sources.py             # quick: one check per panel
python3 /opt/econ/dashboard/sources.py --doctor    # exhaustive: every provider and mapped
                                                   # FRED series/fallback + breaker report

# Offline tests (no network needed; faked transport)
cd /opt/econ && python3 -m unittest discover -s tests -v

# Check API summary & service logs
curl -s localhost:8080/api/summary | python3 -c "import json,sys; s=json.load(sys.stdin); print('indices:',sum(q is not None for q in s['indices']),'/',len(s['indices']),'| commodities:',sum(q is not None for q in s['commodities']),'/',len(s['commodities']),'| fx:',len(s['fx']),'/',8,'| cpi:',len(s['cpi']),'| gdp:',len(s['gdp']),'| curve pts:',len(s['curve'])); print('errors:',s['errors']); print('providers:',[(p['provider'],p['state'],p['retry_in']) for p in s['providers']])"
journalctl -u econ-dashboard -n 30 --no-pager
```

Preview (no network needed): `ECON_DEMO=1 python3 dashboard/app.py` → sample data.

## 6. Current status

- [x] **PR #3 (v0.3) merged to `main` on 2026-10-08 and deployed on `192.168.0.149`** —
  reported by the owner; the merge/deploy were not independently re-verified from this session.
  The measured `--doctor` output is captured in §9 Session 4.
- [x] v0.3 is running as `econ-dashboard.service` at `/opt/econ` (owner-reported); deployment
  model remains `git reset --hard origin/main && ./install.sh` after an owner merge.
- [x] **v0.4 follow-up implemented on `arena/8d320fd8-econ`; not yet merged or deployed.**
  It wires confirmed FRED series and monthly freshness limits, adds the WTI/Brent replacements
  plus FRED-only aluminum/nickel cards, bounds FRED 404 handling, prevents urllib retries after
  TCP/HTTP-status errors, normalizes Treasury headers, and isolates the Yahoo chart checks.
- [x] Offline verification for this follow-up: **37 stdlib tests pass**; demo `/`, `/api/summary`,
  and `/healthz` all return 200 (9/9 indices, 8/8 commodities, 8/8 FX, CPI/GDP 7/7, 1460 curve
  points, 365 breakeven points, no providers); details and A/B timings are in §9 Session 4.
- [ ] Owner to review/merge the follow-up PR, deploy it from `origin/main`, then run the live
  `--doctor` and API-summary checks in §8. No live provider behavior after this follow-up has been
  verified from the sandbox.

## 7. Known risks / watch-list

- **Yahoo remains unresolved.** The owner's 2026-10-08 run confirmed the cookie handshake but
  got HTTP 429 from `v7/finance/spark`; it did not actually reach the v8 chart tests because the
  shared breaker masked them. This branch changes `--doctor` to test v8 charts with the cookie
  first and reset Yahoo breakers between probes. The sandbox's own doctor run cannot reach data
  hosts, so whether v8 works with the cookie is still unverified.
- **Five indices depend solely on Yahoo**: `^dax`, `^ukx`, `^stx`, `^shc`, and `^hsi` have no
  confirmed FRED equivalent. If v8 chart+cookie fails, probe CNBC's key-less quote/history API
  from the LXC before deciding whether to add it or trim the board; do not infer support.
- **Stooq is dead from the deployed LXC**: the supplied `--doctor` measured 21.06 s for each
  endpoint because the old transport retried TCP timeouts through urllib. This branch stops that
  retry and still runs the 3 s parallel pre-flight probes. The post-fix LXC timings remain
  unverified; keep Stooq as a fallback until the owner re-runs `--doctor`.
- **FRED replacement maps are based on the owner's measured results**, not an agent-network probe:
  Brent daily `DCOILBRENTEU` was fresh; `POILWTIUSDM`, `POILBREUSDM`, `PCOPPUSDM`, `PWHEAMTUSDM`,
  `PALUMUSDM`, and `PNICKUSDM` were live at 99 days old. A 120-day gate now accepts the monthly
  observations. Verify the new map and the Brent monthly fallback after deployment.
- **Treasury breakeven fix is locally tested only.** Header matching is case-insensitive for both
  `10 Yr` and `10 YR`; the real yield-curve CSV still needs a live LXC `--doctor` confirmation.
- **Ad-hoc `/opt/econ` edits** were made during earlier deploy debugging. After the owner merges,
  deploy with `git fetch --all && git reset --hard origin/main` before `./install.sh` so local
  changes cannot block or contaminate the checkout.
- **Sandbox has no egress to data hosts** (only GitHub/PyPI are available). The offline doctor run
  in Session 4 exited 0 while every data source failed gracefully; no live claim after the
  supplied LXC measurements is independently verified.

## 8. Next steps (owner after the follow-up PR)

1. After reviewing and merging the PR, deploy on `root@econ`:
   `cd /opt/econ && git fetch --all && git reset --hard origin/main && ./install.sh`.
2. Re-run `python3 /opt/econ/dashboard/sources.py --doctor`. It now tests the cookie handshake,
   then `chart ^GSPC`, `chart ^GDAXI`, and `chart ^HSI` independently, and only then the spark
   batch. Paste the output back; it is the evidence needed to settle Yahoo.
3. If any v8 chart works with the cookie, prefer chart+cookie in the warm path and keep spark
   optional. If v8 fails, probe `quote.cnbc.com` from the LXC for key-less quote/history support
   before choosing CNBC vs trimming the five Yahoo-only indices. Do not guess.
4. Confirm `ustreasury breakeven` now returns a series, and verify the mapped FRED cards and their
   freshness: WTI monthly `POILWTIUSDM`, Brent daily `DCOILBRENTEU` then monthly `POILBREUSDM`,
   copper/wheat/aluminum/nickel monthly, plus CoinGecko gold/silver. The deployment summary should
   count 9 indices and 8 commodities; the five Yahoo-only indices may remain empty until Yahoo or
   a new provider is confirmed.
5. Review `curl -s localhost:8080/api/summary` and the provider breaker footer; preserve its live
   output in the next handoff rather than extrapolating from the simulation.
6. Ideas pool: global shipping (BDI) panel · debt-to-GDP panel · central-bank meeting calendar ·
   multi-region yield curves · "snapshots" (PNG export for wall display).

## 9. Session log

### 2026-10-08 — Session 4: LXC doctor evidence → confirmed series, bounded transport, Yahoo probe fix

- **Input:** owner supplied the live `root@econ` doctor output captured at `2026-10-08T00:18:04Z`.
  This is the LXC's measured ground truth; the sandbox could not independently re-query data hosts.
  Key results:
  - Frankfurter `.app`/`.dev`: 0.69/0.72 s; World Bank CPI/GDP: 1.81/0.48 s.
  - FRED policy/curve IDs (`DFF`, `ECBDFR`, `T10Y2Y`, `T10YIE`) all worked in about 0.6–0.7 s;
    NY Fed EFFR and Treasury 10y–2y worked. Treasury breakeven failed with `no 10Y breakeven data`
    in 3.81 s and no cause detail.
  - FRED `SP500`, `NASDAQ100`, `DJIA`, `NIKKEI225`, and daily Brent `DCOILBRENTEU` were live.
    `DCOILWTI` returned 404 in 24.47 s. Copper/wheat and the monthly WTI/Brent/aluminum/nickel
    candidates were live, with their latest observations 99 days old. `PSILVUSDM` and
    `GOLDAMGBD228NLBM` returned 404 in 12.60/25.42 s.
  - Yahoo cookie handshake succeeded; spark batch got HTTP 429 in 0.30 s. The old doctor then
    skipped v8 chart tests in 0.00 s because the spark 429 had already tripped Yahoo's breaker;
    chart+cookie behavior remains untested on the LXC.
  - Stooq `.com`, `.pl`, and HTTP `.com` all timed out at about 21.06 s each. CoinGecko
    `pax-gold`/`kinesis-silver` both worked in 0.47/0.54 s.
- **Implemented on `arena/8d320fd8-econ` (v0.4 follow-up, pending owner merge):**
  - FRED: replace WTI `DCOILWTI` with monthly `POILWTIUSDM`; keep daily Brent `DCOILBRENTEU` and
    add monthly `POILBREUSDM` fallback; remove 404 silver/gold FRED candidates; set monthly IMF
    freshness limits to 120 days. Add optional FRED-only aluminum/nickel cards from `PALUMUSDM` and
    `PNICKUSDM`. Gold/silver CoinGecko mappings are unchanged.
  - `_fred_csv_series`: 8 s regular request, one 5 s widen to at most 5 years, then a process-local
    24 h `_FRED_DEAD` negative cache after a confirmed 404. Monthly quote histories request 5 years.
  - `_get`: do not retry TCP failures or completed HTTP errors through urllib; allow only
    TLS/HTTP-protocol fallback and share one total time budget between curl and urllib.
  - Treasury CSV headers are normalized case-insensitively (`10 Yr`/`10 YR`); Yahoo `--doctor`
    now probes cookie-backed v8 charts before spark and clears Yahoo breakers between probes.
  - Updated the simulator with the measured per-provider latencies; it now fakes curl and urllib
    separately so each checkout's real `_get()` fallback policy is exercised.
- **Verification — offline only:**
  - `python3 -m unittest discover -s tests -v` → **37 passed**; `py_compile` passed.
  - Demo mode on port 8091: `/`, `/api/summary`, `/healthz` → **200**; 9/9 indices, 8/8 commodities,
    8/8 FX, 7 CPI, 7 GDP, 1460 curve points, 365 breakeven points, `providers: []`; 29 inline SVGs.
  - `python3 dashboard/sources.py --doctor` in the sandbox exited **0** and failed external checks
    gracefully (no data-host egress). Its Yahoo chart probes were no longer masked by the spark
    breaker, but these failures are not evidence about the LXC.
  - Same simulated LXC network, baseline PR #3 code (`bd8c6d2`) vs this branch: `warm_all()`
    **35.8 s / 14 of 22 keys → 6.3 s / 19 of 24 keys**; indices 4/9 → 4/9, commodities 3/6 → 8/8,
    FX 8/8 and CPI/GDP 7/7 on both. The synthetic sim returns 30 curve/breakeven points; this is a
    transport/cascade A/B, not a live-deploy measurement.
- **Still needs LXC evidence:** rerun `--doctor` after deployment to confirm the new FRED mappings,
  Treasury breakeven, Stooq cost, and especially whether v8 chart works with the cookie. Follow
  §8; do not infer an answer from the sandbox.

### 2026-10-08 — Session 3: v0.2 live-deploy diagnosis → v0.3 (breakers, batched Yahoo, FRED gate) v0.2 live-deploy diagnosis → v0.3 (breakers, batched Yahoo, FRED gate)
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
- **Follow-up (2026-10-08):** owner merged PR #3 into `main` (branch commit `bd8c6d2`) and
  deployed it. This is recorded from the owner's report; the session that merged it lost
  GitHub access afterward, so the merge was not independently verified here.

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
