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
| Working branch | `arena/697136fe-econ` (session branch; merge to `main` via PR) |
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

Data-source rules: free, no signups; every source fails independently (panel shows
"awaiting data"/stale badge, board never breaks); polite fetch cadence via cache TTLs.

## 4. Architecture (v0.2)

```
dashboard/
  app.py        HTTP server (ThreadingHTTPServer) + disk-backed cache + 4-worker refresher
  config.py     ALL panels/symbols/TTLs/FRED_QUOTES — edit this to change the board
  sources.py    multi-source fetchers + `curl -4` transport + CLI self-test (`python3 sources.py`)
  demo.py       same signatures as sources.py → synthetic data (ECON_DEMO=1)
  charts.py     server-side SVG: sparkline(), line_chart()
  render.py     single-page HTML/CSS builder (dark theme, source & stale badges, auto-refresh)
systemd/econ-dashboard.service
install.sh      idempotent server bootstrap (apt python3 + curl → unit → enable → restart)
uninstall.sh
.env.example    optional overrides + reserved FRED key slot
```

- `/` board · `/api/summary` JSON · `/healthz` probe
- Background thread pool (`max_workers=4`) warms fast macro sources (`fx`, `wb_cpi`, `wb_gdp`,
  policy rates, yield curve, breakeven) **first**, then market quotes (`INDICES`, `COMMODITIES`)
- Disk cache (`/var/tmp/econ-dashboard-cache.json`) keeps last good data across `systemctl restart`
- Failed fetch keeps last good data and surfaces a ⚠ source-issue line in the footer
- Charts are inline SVG — **no CDN, works fully offline on LAN** once data is cached

## 5. Ops runbook (on the server)

```bash
# Deploy latest changes (overwrites any local ad-hoc edits in /opt/econ)
cd /opt/econ
git fetch --all
git reset --hard origin/main    # or origin/arena/697136fe-econ before PR merge
./install.sh

# Test upstream data sources directly from the LXC
python3 /opt/econ/dashboard/sources.py

# Check API summary & service logs
curl -s localhost:8080/api/summary | python3 -c "import json,sys; s=json.load(sys.stdin); print('indices:',sum(q is not None for q in s['indices']),'/',len(s['indices']),'| commodities:',sum(q is not None for q in s['commodities']),'/',len(s['commodities']),'| fx:',len(s['fx']),'/',8,'| cpi:',len(s['cpi']),'| gdp:',len(s['gdp']),'| curve pts:',len(s['curve'])); print('errors:',s['errors'])"
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
- [ ] Pull v0.2 on `192.168.0.149` (`git fetch --all && git reset --hard ... && ./install.sh`) and verify all sections live

## 7. Known risks / watch-list

- **Ad-hoc edits on `/opt/econ`**: During Session 1 post-merge debugging, heredoc patches were
  pasted directly into `/opt/econ/dashboard/{config,sources,app,render}.py`. Always use
  `git fetch --all && git reset --hard origin/main` (or the session branch) before `./install.sh`
  so git doesn't refuse to pull over local modifications.
- **Yahoo Finance rate limits**: Mitigated via `_YAHOO_LOCK` (0.45s pacing), browser UA,
  `query2`/`query1` failover, and automatic fallback to FRED/Stooq/CoinGecko.
- **FRED `fredgraph.csv` latency**: Mitigated by passing `&cosd=YYYY-MM-DD` (fetching 1–4 years
  instead of 70 years of daily rows), using `curl -4` HTTP/2, and falling back to NY Fed, ECB,
  and US Treasury official endpoints.

## 8. Next steps (backlog)

1. Run the deploy block in §5 on `root@econ` and run `python3 /opt/econ/dashboard/sources.py` to
   confirm which primary/fallback providers are fastest from the LXC's network.
2. Merge `arena/697136fe-econ` PR into `main`.
3. Ideas pool: global shipping (BDI) panel · debt-to-GDP panel · central-bank meeting
   calendar · multi-region yield curves · "snapshots" (PNG export for wall display).

## 9. Session log

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
