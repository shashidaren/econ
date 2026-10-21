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
| Working branch | `arena/778fffe7-econ` (this session; branched from `main` at `1b44ab2`). **PR #13 merged into `main` as `b6b9977` on 2026-10-21T00:07:36Z**; deployment is now up to the LXC's cron (D21) or a manual pull |
| Deploy model | server does `git fetch --all && git reset --hard origin/main && ./install.sh` — **now also automatic** (D21) |
| Unattended updates | **`/etc/cron.d/econ-update`** → `root /opt/econ/tools/auto_update.sh` every **30 min** (schedule set at install time via `ECON_UPDATE_SCHEDULE='0 * * * *' ./install.sh`), log `/var/log/econ-update.log`, lock `/var/lock/econ-update.lock`. Installed by `install.sh` on every run; removed by `uninstall.sh` or `rm -f /etc/cron.d/econ-update` |
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
| D11 | Yahoo crumb authentication | **Fetch session crumb via `v1/test/getcrumb` using the session cookie and pass `&crumb={crumb}` to chart & spark endpoints** | Live LXC testing proved cookie alone still returns HTTP 429 across all endpoints; modern Yahoo query APIs require a crumb matching the session cookie to authorize requests |
| D12 | CNBC fallback for international indices | **Cascade across `Yahoo -> FRED -> CNBC -> Stooq -> CoinGecko` with 1-request batch caching for unmapped indices** | Euro Stoxx 50, DAX, FTSE 100, Shanghai Composite, and Hang Seng lack FRED coverage. When Yahoo fails, CNBC's key-less quote API provides live prices/changes in a single HTTP call; `--doctor` probes each independently |
| D13 | Post-merge live verdict (2026-10-08 01:13 UTC) | **Yahoo = IP-level HTTP 429 (crumb endpoint itself 429); CNBC = HTTP 500 on all 6 probes; board stays at 4/9 indices** | Both v0.5 hypotheses falsified live on `root@econ`. FRED + Treasury + CoinGecko + Frankfurter + World Bank carry 100% of populated cards. Owner to decide: trim `INDICES` to 4 verified cards vs keep 9 with "awaiting data" vs investigate CNBC 500 root cause / alternative key-less feeds |
| D14 | Investment briefing | **Derived on the server from data already on the board** (`dashboard/briefing.py`), rendered as a strip under the header and included in `/api/summary` as `briefing` | No new upstreams. Posture is Defensive / Cautious / Constructive / Awaiting data from curve, real-rate proxy (Fed funds − 10y breakeven), US CPI, GDP breadth, and populated index breadth. Explicitly not advice. Empty inputs become muted cards |
| D15 | Five missing indices | **Tencent then Sina, after CNBC, only for `^shc` `^hsi` `^ukx` `^dax` `^stx`** | 2026-10-08 probe: Tencent `sh000001` / `hkHSI` / `ukUKX` are live or previous close; Sina `b_DAX` / `b_SX5E` cover the two Tencent misses. Do not use Sina `int_dax`, `int_ftse` (stale 9284 vs 10458), or `int_fsx5e` (empty). Eastmoney was HTTP 502. GBK pages; parser keeps ASCII prices. Card source reads `tencent:…` or `sina:…` |
| D16 | Gold inflation read | **FRED `DFII10` 10y real yield chart plus a briefing card** | Owner invests mostly in gold. Breakeven is expected inflation; the real yield is the opportunity cost. Card uses DFII10, else funds − breakeven. ≥1.5 headwind, 0.5–1.5 caution, negative is the classic tailwind. Not a price forecast |
| D17 | Summary tab | **`/summary` page, same cache as `/`** | Second tab, not a new fetch. Posture, briefing lines, rates, and the loaded quotes/CPI/GDP in prose. Board tab links to it |
| D18 | Short-term stock watchlist | **`/stocks` + `/api/stocks`, every passer up to 7 U.S.-listed names under $50** (was 3; raised 2026-10-20) | Editable 32-symbol universe; $5 floor, $5M 20-session average dollar-volume floor, at least 61 sessions, daily close no more than 5 calendar days old, positive 20-session return and close above the 20-day average. Rank 5/20/60-session momentum with volatility/drawdown penalties. Low/Medium/High are backward-looking realized-risk buckets only. Tencent K-line → batched Yahoo → Stooq; no fundamentals, news, earnings calendar, analyst targets, or intraday data. Not a buy signal. Display cap is `ECON_STOCK_MAX_RESULTS` (default 7, hard-clamped to 32); the full `eligible_count` is reported and printed on the page even when the list is capped |
| D19 | Moomoo Malaysia use | **Read-only, no broker integration; preserve the editable U.S. universe and existing limits** | Moomoo MY's official help center says fractional availability is limited to selected U.S. securities and its in-app eligibility list may change. This app cannot validate live per-symbol tradability. Show an optional fresh ECB/Frankfurter USD/MYR reference with source/date/fetch time and prominent non-executable/no-fees caveats; link the official fee schedule rather than calculating charges |
| D20 | Stocks-tab update cadence (verified 2026-10-20) | **Keep the 1 h screen TTL (daily-close data does not justify more); make the cadence visible on the page and in the API** | Owner asked whether the screen updates often enough. Measured in code: board cycle 300 s, `TTL["stocks"]` 3600 s, and a failed refresh is skipped until it is 1 h old; the inputs are *daily* bars that only change after the U.S. close (~04:00–05:00 MYT). So the list legitimately sits still between closes. Instead of hammering keyless providers 12× more often for no new information, `/stocks` now prints "Refresh cadence: providers re-checked at most every 60 min · next re-check in about N min · the underlying daily bars only change after the U.S. close, so this list is not intraday". `/api/stocks` and `/api/summary.stock_screen` expose `refresh_interval_seconds`, `checked_at_utc`, and `next_check_in_seconds`. Intraday movement would need a new keyless intraday source, which does not exist in this pipeline (owner call) |
| D21 | Unattended updates (2026-10-21) | **cron → `tools/auto_update.sh`, tracking `origin/main` only; pull + restart, no test gate; `install.sh` always installs `/etc/cron.d/econ-update`** | Owner wants zero manual deploy management. Behaviour chosen explicitly: main only (merged/reviewed work only), `git reset --hard origin/main` (same convergence rule as the manual deploy, so hand edits in `/opt/econ` are discarded), restart **only** when the commit actually changed, `flock` serialisation, dry-run mode. The owner declined the verify-then-rollback option, so the only gate before restart is `install.sh`'s import smoke test: a commit that breaks Python imports leaves the running service on the old code and logs an error. Trade-off accepted: whatever lands on GitHub `main` deploys itself to the box as root within ~30 min (§7) |
| D22 | Stock headlines (2026-10-21) | **Key-less RSS per displayed ticker: Google News → Bing News → Yahoo Finance headline feed; context only, never scored; at most 3 items inside 7 days** | The owner asked for recent news on the stocks the dashboard selects. No new key or login: all three are public RSS, parsed with `xml.etree.ElementTree` (RSS 2.0 + Atom, namespace-tolerant). One request per displayed ticker per hourly screen refresh (≤7 small requests/h). Filters are deliberately conservative because there is no relevance oracle: keep only items whose publication date is inside the window (undated items dropped, never shown as "recent"), whose title mentions the ticker as a word or a distinctive part of the company name, deduplicated by normalised title; publisher suffix stripped only when the feed states the publisher. A failure is a visible per-symbol sentence, never a silent empty card, and news can never fail or change the screen. `--doctor` §[9/9] probes the three feeds. Toggle `ECON_STOCK_NEWS=0`, window `ECON_STOCK_NEWS_DAYS` (1–30), cap `ECON_STOCK_NEWS_PER_SYMBOL` (1–5) |
| D23 | Seven-day price movement (2026-10-21) | **Derived from the daily bars already fetched: `7 days` = last close at or before (latest bar − 7 calendar days) → latest close, plus a 12-session sparkline, 7-day high/low and a breadth strip over the displayed names** | The owner asked for price movement "within 7 days". No new upstream and no intraday claim: the window ends at the latest daily bar, so it normally spans five U.S. sessions and never includes days the pipeline does not have. Per-card `ret7_pct`, `week_start/end`, `week_high/low`, `recent_bars` and a screen-level `week` block ({average, up/down/flat, best, worst}) are in `/api/stocks` and `/api/summary.stock_screen`. Descriptive only — no forecast, no effect on filters or rank |

Data-source rules: free, no signups; every source fails independently (panel shows
"awaiting data"/stale badge, board never breaks); polite fetch cadence via cache TTLs.

## 4. Architecture (Stocks watchlist merged; headlines + 7-day movement on `arena/778fffe7-econ`)

```
dashboard/
  app.py        HTTP server (ThreadingHTTPServer) + disk-backed cache + 4-worker refresher
                + pre-flight step: warm_providers() then prefetch_yahoo() before the fan-out
  config.py     panels/symbols/TTLs plus the 32-symbol US stock watchlist and filters
  sources.py    quote/rates cascades + transport/breakers + Tencent US daily K-lines,
                volume-aware Yahoo batch and Stooq .us fallback for the stock screen;
                CLI diagnostics (`python3 sources.py` / `--doctor`)
  demo.py       same backend signatures with synthetic data (ECON_DEMO=1); stock bars
                are explicitly demo-only and must never be treated as live picks
  charts.py     server-side SVG: sparkline(), line_chart()
  render.py     single-page HTML/CSS builder (dark theme, source & stale badges,
                investment-briefing strip under the header,
                footer "source issues" + "upstream health" lines, auto-refresh);
                Stocks tab renders the 7-day movement strip, per-card sparkline and
                linked headline list (http(s)-only hrefs, escaped text)
  briefing.py   posture + signal cards from the summary dict already in memory
                (curve, policy real-rate proxy, CPI, GDP, index breadth). No I/O
  news.py       recent headlines for the displayed tickers: Google News RSS ->
                Bing News RSS -> Yahoo Finance RSS, window/relevance/dedup filters,
                per-symbol errors, shared provider breakers. Context only — the
                screen never reads it back
  stocks.py     technical watchlist scoring, $5-$50/volume/freshness/trend filters,
                observed volatility/drawdown risk buckets, every-passer output capped
                at MAX_RESULTS=7 (ceiling 32); seven-day return/range/recent bars and
                a screen-level `week` breadth summary
  app.py        also stamps the stock screen with its refresh cadence
                (`_with_stock_refresh_meta`: interval, last check, next check)
tests/test_sources.py    47 stdlib unittests — cascade, breakers, FRED gate/cache, transport,
                         Yahoo cookie+crumb, CNBC parsing & caching, Treasury headers, rendering
tests/test_stock_screen.py 26 stdlib unittests — filters, rank cap (7/ceiling), freshness,
                           risk labels, refresh cadence, Tencent/Yahoo parser, provider fallback
tests/test_stock_news.py  27 stdlib unittests — RSS/Atom parsing, window/relevance/dedup,
                          fallback cascade + breakers, unsafe links, 7-day metrics and
                          breadth, page rendering and escaping; no network
tests/test_briefing.py   4 stdlib unittests — defensive/constructive/empty posture + page strip
tests/test_asia_quotes.py 2 stdlib unittests — Tencent/Sina parsers and cascade preference
tests/test_auto_update.sh 17 bash assertions — offline, throwaway git repos: up-to-date
                           no-op, update applies + runs install.sh, dry run, wrong-branch
                           refusal, local edits discarded, lock contention (bash tests/test_auto_update.sh)
tools/sim_lxc_network.py replays the LXC's measured latencies against the real cascade
tools/warm_ab.py         times warm_all() for any checkout (A/B comparisons)
tools/auto_update.sh     unattended updater for cron (D21): fetch -> compare with
                         origin/main -> reset --hard -> install.sh, only on change;
                         flock-serialised, dry-run mode, refuses the wrong branch
systemd/econ-dashboard.service
install.sh      idempotent server bootstrap (apt python3 + curl → unit → enable → restart)
uninstall.sh
.env.example    optional overrides + reserved FRED key slot
```

- `/` board · `/summary` macro summary · `/stocks` ranked watchlist (every passer, up to 7,
  with 7-day movement and recent headlines) · `/api/summary` JSON (includes `stock_screen`) ·
  `/api/stocks` screen JSON (`week`, `news`) · `/healthz` probe
- Stock screen cache TTL: 1 h (page shows the cadence and next check time); US daily K-lines are
  retrieved with four concurrent Tencent requests
  at a time, then Yahoo is batched and Stooq is per-symbol fallback. It only returns current,
  sufficiently liquid names passing the configured price/trend filters.
- Each refresh cycle: **pre-flight** (probe stooq hosts 3 s, one batched Yahoo spark request for
  stale Yahoo-mapped symbols) → 4-worker fan-out over fast macro sources first, then market quotes
- Disk cache (`/var/tmp/econ-dashboard-cache.json`) keeps last good data across `systemctl restart`
- Failed fetch keeps last good data and surfaces a ⚠ source-issue line in the footer
- Charts are inline SVG — **no CDN, works fully offline on LAN** once data is cached

## 5. Ops runbook (on the server)

**Deployment is now automatic (D21).** Once `install.sh` has been re-run on the LXC,
`/etc/cron.d/econ-update` runs `tools/auto_update.sh` every 30 min and deploys whatever is on
`origin/main` — no approval step per change. The second caveat still stands: the current Arena
follow-up branch is **not** on `main`, so nothing from it reaches the box until the PR is merged;
do not point the cron branch at a session branch during development.

```bash
# Automatic path (what cron does): fetch -> compare -> reset --hard -> install.sh, on change only
sudo /opt/econ/tools/auto_update.sh                          # run it now
ECON_UPDATE_DRY_RUN=1 sudo /opt/econ/tools/auto_update.sh    # report only, change nothing
tail -f /var/log/econ-update.log                             # update log (UTC timestamps)
sudo rm -f /etc/cron.d/econ-update                           # stop unattended updates again

# Manual deploy (still valid; overwrites local edits in /opt/econ)
cd /opt/econ
git fetch --all
git reset --hard origin/main
./install.sh

# Change the auto-update schedule (then re-run install.sh, or just edit the cron file)
sudo ECON_UPDATE_SCHEDULE='0 * * * *' /opt/econ/install.sh   # hourly instead of every 30 min

# Test upstream data sources directly from the LXC
python3 /opt/econ/dashboard/sources.py             # quick: one check per panel
python3 /opt/econ/dashboard/sources.py --doctor    # exhaustive: every provider and mapped
                                                   # FRED series/fallback + breaker report;
                                                   # §[9/9] probes the three stock headline feeds

# Offline tests (no network needed; faked transport)
cd /opt/econ && python3 -m unittest discover -s tests -v

# Check API summary, stock screen (up to 7 passers) and its refresh cadence, and service logs
curl -s localhost:8080/api/stocks | python3 -c "import json,sys; s=json.load(sys.stdin); print(s['status'], '|', s['message']); print('data date', s['data_date'], '| source', s['source']); print('checked', s.get('checked_at_utc'), '| next check in', s.get('next_check_in_seconds'), 's | interval', s.get('refresh_interval_seconds'), 's'); w=s.get('week') or {}; print('7-day: avg', w.get('average_ret7_pct'), '| up/down', w.get('up'), w.get('down'), '| best', w.get('best'), '| worst', w.get('worst')); n=s.get('news') or {}; print('news:', n.get('status'), '|', n.get('message'), '|', (n.get('coverage') or {}).get('with_headlines'), 'of', (n.get('coverage') or {}).get('symbols'))"
curl -s localhost:8080/api/stocks | python3 -c "import json,sys; s=json.load(sys.stdin); [print(c['symbol'], 'ret7', round(c.get('ret7_pct') or 0,2), '|', len((s.get('news') or {}).get('items',{}).get(c['symbol'],[])), 'headlines') for c in s.get('candidates',[])]"
curl -s localhost:8080/api/summary | python3 -c "import json,sys; s=json.load(sys.stdin); print('indices:',sum(q is not None for q in s['indices']),'/',len(s['indices']),'| commodities:',sum(q is not None for q in s['commodities']),'/',len(s['commodities']),'| fx:',len(s['fx']),'/',8,'| cpi:',len(s['cpi']),'| gdp:',len(s['gdp']),'| curve pts:',len(s['curve'])); print('stock screen:',s['stock_screen']['status'],s['stock_screen']['source'],s['stock_screen']['data_date']); print('errors:',s['errors']); print('providers:',[(p['provider'],p['state'],p['retry_in']) for p in s['providers']])"
curl -s localhost:8080/api/stocks | python3 -m json.tool
journalctl -u econ-dashboard -n 30 --no-pager
```

Preview (no network needed): `ECON_DEMO=1 python3 dashboard/app.py` → sample data.

## 6. Current status

- [x] **Recent headlines + 7-day price movement added (this session), merged in PR #13 as `b6b9977` (2026-10-21T00:07:36Z).** Not yet confirmed on the LXC: the cron entry deploys `main` within ~30 min once `install.sh` has been run there; verify with `--doctor` §[9/9] and a `/stocks` load (§8).
  `/stocks` now shows a 7-day movement strip (average, up/down/flat, strongest/weakest over the
  displayed names), a per-card 12-session sparkline, `1 day`/`7 days`/`20 sessions`/`60 sessions`
  stats, the 7-day high/low range, and up to three recent headlines per ticker. New
  `dashboard/news.py` (Google News RSS → Bing News RSS → Yahoo Finance RSS) with a 7-day window,
  ticker/company relevance, title dedup, per-symbol error strings, and shared circuit breakers;
  `--doctor` gained §[9/9]. `stocks.py` computes `ret7_pct`/`week_start`/`week_end`/
  `week_high`/`week_low`/`recent_bars` and a screen-level `week` summary. Filters, ranking, risk
  buckets, universe, provider cascade for prices, and the 1 h screen TTL are unchanged. Headlines
  are context only: they are fetched **after** ranking and are never read by the screener.
  `ECON_STOCK_NEWS=0` disables them; `ECON_STOCK_NEWS_DAYS` (1–30) and
  `ECON_STOCK_NEWS_PER_SYMBOL` (1–5) tune them.
- [x] **Tests:** **106/106** stdlib unittests pass in this checkout (79 before this session; 27 new
  in `tests/test_stock_news.py`), plus `py_compile`. Demo headlines are generated locally
  (`DEMO headline …`, no links) and are not evidence of real news.
- [x] **Unattended auto-update added (D21), not yet active on the LXC:** `tools/auto_update.sh` + `install.sh` now always writes `/etc/cron.d/econ-update` (default `*/30 * * * *`, log `/var/log/econ-update.log`). It tracks **`origin/main` only**, so this branch's changes reach the box only after PR #11 is merged. **The owner must run `./install.sh` once on `192.168.0.149` to create the cron entry** — until then nothing changes on the server.
- [x] **Stocks display cap raised 3 → 7 (this session, `arena/83ffb2c7-econ`), not deployed.** `/stocks`, `/api/stocks`, and `/api/summary.stock_screen` now list **every** ticker that passes, ranked, up to `ECON_STOCK_MAX_RESULTS` (default 7, hard-clamped to 32). Filters, ranking, risk buckets, and the universe are unchanged; `eligible_count` is still reported in full and printed on the page when the list is capped. Nothing else about the screen changed.
- [x] **Update-frequency question answered (D20), not deployed.** The screen re-checks providers at most every 60 min (`TTL["stocks"]`) on the 5-min board cycle, and a failed refresh is skipped until it is 1 h old; its inputs are *daily* bars, so the numbers only change after the U.S. close (~04:00–05:00 MYT). Kept the 1 h TTL and instead surfaced the cadence in the UI and the API (`refresh_interval_seconds`, `checked_at_utc`, `next_check_in_seconds`).
- [x] **Stocks baseline is merged in `cddd673` and owner-reported live on the LXC:** as of 2026-10-13, the page reports 32/32 tickers with usable history, latest daily bar 2026-10-12, Tencent K-line, seven screen-passers and at most three displayed. This is owner-provided status, not independently queried from this sandbox.
- [ ] **Earlier Stocks follow-ups on `arena/a5e9894b-econ` are not deployed:** explicit MYT/U.S. Eastern clocks, optional fresh-reference MYR equivalents, Moomoo Malaysia availability/fee links and limitations, minimum-history-based coverage, and clearer partial-provider diagnostics. No broker integration or order automation.
- [x] **Summary tab, Gold read, and recession meter are present in this checkout.**
- [x] **Tests:** merged Stocks baseline passed 63 tests; the previous follow-up passed 70; **this checkout passes 106/106** standard-library tests plus `py_compile`. Demo quote and headline data are synthetic and are not evidence of live picks or live news.
- [x] **Tencent/Sina index fallback (v0.7) deployed on `192.168.0.149` at `5fd45e6`.**
  Doctor `2026-10-08T02:09:21+00:00` §[8/8] **5/5 passed**: Shanghai `tencent:sh000001`
  3852.02 @ 2026-10-08, Hang Seng `tencent:hkHSI` 24069.56 @ 2026-10-08, FTSE
  `tencent:ukUKX` 10458.5 @ 2026-10-07, DAX `sina:b_DAX` 25104.36 @ 2026-10-07,
  Euro Stoxx `sina:b_SX5E` 6180.3 @ 2026-10-08. Yahoo still 429, CNBC still 500, Stooq
  still timeout; NY Fed and ECB fallbacks green again (8/8). Board should now be 9/9 indices.
- [x] **Investment briefing (v0.6) merged as PR #7 (`c2dc6b9`) and seen live** at
  `http://192.168.0.149:8080` on 2026-10-08 01:31 UTC — posture Cautious, curve 0.51%,
  Fed funds 3.88%, real-rate proxy 1.52%, markets 1/4 up with 5 index cards awaiting data.
- [x] **PR #5 (v0.5) merged to `main` (`e22471c`) and deployed on `192.168.0.149`** —
  confirmed by owner's `--doctor` output at `2026-10-08T01:13:43+00:00`
  (this run has the 7-section doctor with `crumb handshake` + §[6/7] CNBC probes, i.e. v0.5 code).
- [x] **Live post-merge measurements (2026-10-08T01:13:43+00:00) — both v0.5 hypotheses falsified:**
  * FX & macro: **4/4 passed** — stable. Frankfurter `.app` 1.08 s / `.dev` 0.33 s
    (-> 2026-10-07); World Bank CPI 0.15 s (-> 2.95), GDP 0.13 s (-> 2.16). Unchanged values.
  * Rates & curve: **6/8 passed** (was 8/8 at 00:52). FRED primaries all OK in 0.13–0.51 s
    (`DFF` 3.88 @ 2026-10-06, `ECBDFR` 2.5 @ 2026-10-07, `T10Y2Y` 0.51, `T10YIE` 2.36) and both
    Treasury fallbacks OK (10y-2y 1.77 s -> 0.51, breakeven 2.91 s -> 2.36). **New failures are
    fallback-only:** `nyfed EFFR` SSL connection timeout (5.01 s) and `ecb DFR` operation timeout
    (10.01 s) — board unaffected while FRED primaries answer, but re-probe to see if transient.
  * FRED quote series: **11/11 passed** (0.10–0.56 s) — identical to 00:52 run. Daily Brent
    125.44 (2d old), DJIA 51179.87 / SP500 7801.77 / NIKKEI225 70035.71 (1d old),
    NASDAQ100 31224.69 (2d old); monthly IMF set still 99d old, accepted by the 120-day gate.
  * Yahoo Finance: cookie set (1.23 s) but **`crumb handshake` itself returns HTTP 429**
    (0.23 s, `query2`) — proving an **IP-level rate limit**, not a missing-auth problem.
    All v8 charts (`^GSPC` 0.35 s, `^GDAXI` 1.64 s, `^HSI` 1.48 s) and spark batch (1.18 s)
    still 429. Breaker: `yahoo: cooling retry_in=879s`.
  * CNBC: **0/6 passed — all HTTP 500** (not the predicted 403): `.GDAXI` 2.58 s, `.FTSE` 0.15 s,
    `.HSI` 0.11 s, `.SSEC` 0.12 s, `.STOXX50E` 0.09 s, batch 1.18 s. Fast failures suggest the
    request reaches CNBC but the server errors — symbol format / params / endpoint drift suspect,
    not a timeout. Breaker: `cnbc: degraded retry_in=0s`.
  * Stooq still dead (3× 5.01 s connect timeouts). CoinGecko live: PAX Gold 4133.21 (+0.2% vs
    4123.7), Kinesis Silver 60.79 (+1.0% vs 60.2), both <0.5 s and dated 2026-10-08.
  * Board coverage on LXC (unchanged from PR #4): Commodities 8/8, FX 8/8, CPI 7/7, GDP 7/7,
    Rates & Curve 100% (via FRED + Treasury). **Indices still 4/9** — `^stx`, `^dax`, `^ukx`,
    `^shc`, `^hsi` remain unpopulated.
- [x] Session 6 (`arena/cda0bd47-econ`): **docs-only** — no code changes. Handoff updated with
  the post-merge verdict; D13 recorded; §7/§8 rewritten around the trim-vs-investigate decision.
- [x] This session (`arena/invest-briefing`): briefing strip + `/api/summary.briefing` + tests.
  Does not change the index-coverage decision in §8.
- [x] The old index-coverage decision no longer blocks work: the LXC later confirmed 9/9 indices via Tencent/Sina. The historical CNBC/Yahoo/Stooq failures remain relevant to source fallback expectations.

## 7. Known risks / watch-list

- **Stocks screen is not investment advice or a forecast:** it uses daily-price momentum and trailing liquidity/volatility/drawdown only. It does not screen valuation, earnings, balance-sheet strength, news, or upcoming catalysts. Price below $50 is not a valuation signal; user should verify quote/earnings/news before any trade.
- **Headlines are context, not a signal (new, D22):** the RSS feeds are third-party and unverified; titles can be syndicated, delayed, clickbait, or about a similarly named company. The relevance filter (ticker as a word, company name, or distinctive token) is a string heuristic, not a semantic one; Google's trailing " - Publisher" is stripped, but Bing/Yahoo titles can keep that suffix because those feeds do not expose the publisher. The screen never reads headlines back, so a wrong headline cannot change which tickers pass — but it can mislead a human. Always open the article before acting, and treat a missing headline list as "nothing found", not "nothing happened".
- **Headline feeds are unverified live from this sandbox:** all three hosts (`news.google.com`, `www.bing.com`, `feeds.finance.yahoo.com`) are unreachable from the agent environment (TLS handshake failure), so the parsers, cascade and UI are covered only by faked-transport tests. First live evidence must come from `python3 /opt/econ/dashboard/sources.py --doctor` §[9/9] on the LXC. If all three are blocked there, the page says "Headlines unavailable" and the rest of the tab is unaffected — do not paper over it with scraped/undocumented endpoints (see D15's history with CNBC 500s).
- **Seven-day movement is derived from daily bars, not intraday:** it compares the latest close with the last close at or before seven calendar days earlier (~5 U.S. sessions). A holiday, a missed session, or a stale-but-allowed quote shifts the window; the page prints the exact start/end dates so the reader can see it. It says nothing about the current bid/ask or today's move.
- **A longer list is a wider watchlist, not a stronger signal (new 2026-10-20):** the display cap moved 3 → 7, so every filter-passer is now visible. Ranking is *relative* to this 32-name universe and the filters were not loosened, so names at the bottom of the list clear the same gates by a smaller margin and need the same research. Rank #7 is not "seventh best in the market".
- **The stocks list is daily, not intraday (D20):** daily closes only appear after the U.S. close (~04:00–05:00 MYT) and the screen re-checks providers hourly, so an unchanged list during a U.S. session is expected, not a stuck feed. Never read these numbers as live quotes; check price, spread, and news in the broker before acting.
- **Moomoo per-symbol availability is not programmatically verified:** the official Malaysia guide says fractional trading is limited to selected U.S. securities and the in-app list can change. The static watchlist must remain editable; users need to confirm a ticker and any fractional eligibility in the app. Do not imply broker support or live orderability.
- **Stock history providers can still fail:** owner reports Tencent covered all 32 configured names on 2026-10-13, but Yahoo/Stooq remain fallbacks and this sandbox cannot query the LXC or market providers. The page must show partial coverage/errors and suppress stale picks, never substitute demo data.
- **Yahoo = IP-level HTTP 429 (settled 01:13 UTC):** `crumb handshake` itself returns 429 in
  0.23 s, so the LXC's IP is rate-limited at Yahoo's edge — no cookie/crumb/pacing tweak on this
  IP will fix it. Yahoo stays in the cascade (breaker-cooled, ~0 ms cost) in case the limit lifts,
  but the board cannot depend on it. Re-probe after a long cooldown to confirm; otherwise the only
  Yahoo paths are a different egress IP or a proxy (owner call).
- **CNBC = HTTP 500 on all 6 probes (new, needs root-cause):** predicted failure was 403
  (datacenter block); actual is fast 500s (0.09–2.58 s), which means the request reaches CNBC but
  the server errors. Prime suspects, in order: (a) symbol format rejected (`.GDAXI`-style `symbolType=issue`
  params no longer accepted — try `GDAXI`, exchange-suffixed, or single-symbol requests);
  (b) endpoint/param drift (`quote-html-webservice/quote.htm?...&requestMethod=quick` changed);
  (c) missing headers/cookies CNBC now requires; (d) IP-based abuse response surfaced as 500.
  The 500 response *body* is not logged today — capturing it is step 1 (§8).
- **International-index failure is historical:** the 2026-10-08 01:13 snapshot was 4/9; the later 02:09 Tencent/Sina LXC doctor passed 5/5 missing-index probes and restored expected 9/9 coverage. Recheck only if current cards regress.
- **NY Fed / ECB fallback timeouts (new, fallback-only):** `nyfed EFFR` SSL timeout (5.01 s) and
  `ecb DFR` 10 s timeout at 01:13 UTC, after passing at 00:52. FRED primaries + Treasury CSVs
  cover the board, so no user impact — but if persistent, the rates cascade loses redundancy.
  Could be transient congestion, IPv4/TLS path issues, or LXC upstream flakiness. Re-probe first.
- **Stooq verified dead from the deployed LXC:** 3× 5.01 s connect timeouts. Breakers + parallel
  3 s pre-flight keep it at ~0 ms during regular warming.
- **Solid core (do not touch):** FRED 11/11 quotes + 4/4 policy/curve primaries, Treasury 10y-2y +
  breakeven, Frankfurter `.app`/`.dev`, World Bank CPI/GDP, CoinGecko gold/silver — all green with
  sub-second to ~3 s latencies and fresh dates.
- **Unattended auto-deploy is a real trust boundary (new, D21):** from the moment the cron entry exists, **anything merged or pushed to GitHub `main` runs as root on the LXC within ~30 min**, including `apt-get` and a service restart. Guard it accordingly: keep the repo private or restrict write access, enable 2FA, and review PRs — `main` is now a production deploy path, not a staging area. Mitigations in place: branch guard (refuses to run unless the checkout is on `main`), `flock`, restart only on a real commit change, and `install.sh`'s import smoke test as a last gate. Reversible any time with `rm -f /etc/cron.d/econ-update`.
- **A bad commit on `main` can take the dashboard down until someone intervenes:** there is no test-suite gate and no rollback (owner's choice, D21). The updater logs the failure and `journalctl -u econ-dashboard` shows why; recovery is `cd /opt/econ && git reset --hard <last-good-sha> && ./install.sh`, with the cron entry removed first so it does not re-apply the bad commit. If unattended safety later matters more than immediacy, add a `python3 -m unittest discover -s tests` + `py_compile` gate and `git reset --hard <previous>` on failure inside `tools/auto_update.sh`.
- **Hand edits inside `/opt/econ` are now destroyed automatically:** each run does `git reset --hard origin/main`, so any local patch or ad-hoc experiment on the server is wiped within 30 min. Edit in git and push, or stop the cron job while experimenting.
- **Sandbox has no egress to data hosts:** only GitHub/PyPI are reachable from this environment.
  Confirmed again this session: `news.google.com`, `feeds.finance.yahoo.com`, `proxy.finance.qq.com`
  and even `qt.gtimg.cn` fail at the TLS handshake with `SSL_ERROR_SYSCALL` from the sandbox.
  All local tests use faked transports; live provider behavior must be verified on `root@econ`.

## 8. Next steps (PR #13 merged as `b6b9977`; verify the automatic deploy on the LXC)

1. **Deploy:** if the owner has run `./install.sh` once on `192.168.0.149`,
   `/etc/cron.d/econ-update` deploys the merge automatically within ~30 min (D21); check
   `tail -20 /var/log/econ-update.log`. Otherwise pull manually
   (`cd /opt/econ && git fetch --all && git reset --hard origin/main && ./install.sh`).
2. **First live check of the headline feeds:** `python3 /opt/econ/dashboard/sources.py --doctor`,
   section **[9/9] Stock headline feeds**. Expect Google News to answer; Bing/Yahoo are fallbacks.
   A blocked provider cools down for 30 min and is skipped. Do this before judging the page — the
   sandbox has no egress to any of the three hosts, so the only evidence so far is faked-transport tests.
3. **Check `/stocks` after the refresh:** the 7-day strip should show an average and up/down counts,
   each card should have a sparkline, `7 days` stat, 7-day range, and up to three linked headlines
   with publisher + age. Confirm a feed failure shows "Headlines unavailable" / per-ticker detail
   **without** blanking the cards, and that `/api/stocks` exposes `week` and `news`.
4. **Owner tuning (env only, no code):** `ECON_STOCK_NEWS=0` disables headlines;
   `ECON_STOCK_NEWS_DAYS=14` widens the window; `ECON_STOCK_NEWS_PER_SYMBOL=2` shortens each card.
   All require a service restart (`systemctl restart econ-dashboard`).
5. **Inspect the first real headline list** (fourth live dimension). If Google/Bing return off-topic
   items, tighten `news.is_relevant` (the stoplist / distinctive-token rule) rather than adding
   another source. If Yahoo headline RSS is 429 on this IP, it can be dropped from `_URL_BUILDERS`
   without touching the screen.
6. The earlier CNBC/Yahoo/Stooq index investigations remain historical. Tencent/Sina restored 9/9
   index coverage in the 2026-10-08 LXC doctor run; revisit those only if coverage regresses.
7. Recovery if a commit breaks the dashboard:
   `rm -f /etc/cron.d/econ-update && cd /opt/econ && git reset --hard <last-good-sha> && ./install.sh`.

## 9. Session log

### 2026-10-21 — Session 15: recent stock headlines + 7-day price movement (D22, D23)

- **Ask:** "If we can have like some recent news on the stocks selected on the dashboard and maybe
  some price movement within 7 days will help." (After reading `handoff.md` for the latest state.)
- **Scope chosen:** both additions live on the Stocks tab (`/stocks`), where the selected names are.
  No change to the board, the universe, the filters, the rank score, the risk buckets, the price
  cascade, or the 1 h screen TTL. Headlines are fetched **after** the ranking, from the displayed
  candidates only, and are never read back by the screener.
- **New `dashboard/news.py`** (D22). Cascade per ticker: Google News RSS
  (`news.google.com/rss/search?q="Name" stock when:7d`) → Bing News RSS (`format=RSS`) → Yahoo
  Finance headline RSS (`feeds.finance.yahoo.com/rss/2.0/headline?s=SYM`). All public, key-less
  RSS 2.0; parsed with `xml.etree.ElementTree` (namespace-tolerant, also accepts Atom). Filters:
  publication date inside the window (default 7 days, 1–30), undated items dropped, title must
  mention the ticker as a word / the company name / a distinctive name token, titles deduplicated
  after normalisation, at most 3 per ticker (1–5), newest first. Google's ` - Publisher` suffix is
  removed when the feed states the publisher; Bing/Yahoo do not, so their titles keep it and the
  publisher column names the feed host. Every failure becomes a per-symbol sentence; the shared
  `sources` breakers (`google_news`, `bing_news`, `yahoo_news`) stop hammering a dead feed, and a
  news failure cannot fail the screen. `sources.fetch_news(...)` is a lazy-import wrapper so
  `app.py` can call the same `backend.fetch_news(...)` in live and demo modes.
- **Demo mode** (`demo.fetch_news`) generates locally-seeded `DEMO headline …` items with no links;
  the page keeps its DEMO banner, so a preview can never look like live news.
- **Seven-day movement (D23) in `stocks.py`:** `ret7_pct` compares the latest close with the last
  close at or before (latest bar − 7 calendar days) — normally 5 U.S. sessions — and the same metric
  block carries `week_start`, `week_end`, `week_high`, `week_low`, and `recent_bars` (last 12
  sessions). The screen adds a `week` summary over the **displayed** names: average move, up/down/
  flat counts, strongest and weakest. All of it is descriptive and ends at the latest bar, so it can
  never imply intraday data.
- **UI (`render.py`):** new `stock-week` strip above the cards, per-card sparkline coloured by the
  7-day direction, `1 day` / `7 days` / `20 sessions` / `60 sessions` / volatility / drawdown /
  dollar-volume stats, a 7-day range in the meta line, and a `stock-news` block with linked
  headlines (http(s)-only hrefs, `rel="noopener noreferrer"`, escaped text, age). The intro, method
  panel and footer say plainly that headlines are third-party, unverified, and never part of the
  screen. A news outage or a `ECON_STOCK_NEWS=0` disablement is stated; cards still render.
- **Wiring:** `config.STOCK_NEWS_ENABLED/WINDOW_DAYS/PER_SYMBOL` (env: `ECON_STOCK_NEWS`,
  `ECON_STOCK_NEWS_DAYS`, `ECON_STOCK_NEWS_PER_SYMBOL`); `app.stock_screen_data()` attaches
  `screen["news"]`, with the empty-screen path returning a neutral `waiting` object so the API shape
  is stable; `--doctor` gained §[9/9] and its earlier headings were renumbered `/9`;
  `install.sh`'s import smoke test now imports `news`; README and `.env.example` document the knobs.
- **Verification (offline only — no egress to any news host from this sandbox):**
  `python3 -m py_compile dashboard/*.py tests/*.py` clean; `python3 -m unittest discover -s tests -v`
  **106/106 passed** (79 before; 27 new in `tests/test_stock_news.py` covering RSS+Atom parsing,
  window/relevance/dedup, Google→Bing→Yahoo fallback, breaker skipping, unsafe links, 7-day math and
  breadth, escaping, demo labelling, and the no-news/no-week degrade path). Demo server on `:8094`:
  `/`, `/summary`, `/stocks`, `/api/stocks`, `/api/summary`, `/healthz` all 200; `/stocks` rendered
  7 synthetic cards with the 7-day strip; `/api/stocks` reported `week` (avg +4.47%, 7 up / 0 down)
  and `news` (7/7 tickers, DEMO items). Demo values are synthetic and say nothing about live picks.
- **Merged:** PR #13 (`arena/778fffe7-econ` → `main`) merged as `b6b9977` at 2026-10-21T00:07:36Z.
- **Not done:** no LXC deploy or live feed probe; headline availability from `192.168.0.149`
  remains unverified until `--doctor` §[9/9] runs there. No new dependencies, keys, or services.

### 2026-10-21 — Session 14: unattended cron auto-update (D21)

- **Ask:** "can we also add a git pull to cron job, i will update crontab so i dont have to manage this manually in future".
- **Owner's three choices (asked up front, since this restarts a service unattended):** track **`main` only**; **pull + restart with no test/compile gate** (the verify-then-rollback option was explicitly declined); **`install.sh` always installs the cron entry**.
- **New `tools/auto_update.sh`** (chmod 755): fetch → compare `HEAD` with `origin/main` → `git reset --hard origin/main` → `./install.sh`, and **only when the commit actually changed**, so an idle run never disturbs the dashboard. Extras that come free with doing it sanely: `flock -n` serialisation (an overlapping run skips rather than queues), a branch guard that refuses to run unless the checkout is on the tracked branch (otherwise `git pull` could merge `main` into a feature branch), `ECON_UPDATE_DRY_RUN=1` reporting mode, and UTC-timestamped logging. `git reset --hard` matches the documented manual deploy, so hand edits in `/opt/econ` are discarded by design. Guard the main-guard: `install.sh` still runs its import smoke test before touching systemd, so a commit that breaks Python imports leaves the **running** service on the old code and logs `install.sh failed`.
- **`install.sh`** now has 5 steps: step [4/5] writes `/etc/cron.d/econ-update` (`SHELL`, `PATH`, `*/30 * * * * root /opt/econ/tools/auto_update.sh >> /var/log/econ-update.log 2>&1`, file `644`), chmods the script `755`, and touches the log. `cron` was added to the apt line so the drop-in actually runs. Schedule overridable: `ECON_UPDATE_SCHEDULE='0 * * * *' ./install.sh`. **`uninstall.sh` removes the cron file too.** Cron filename has no dot (run-parts silently ignores dotted names).
- **Verification:** `bash -n` clean on all four shell files. New `tests/test_auto_update.sh` — **17/17 assertions pass**, fully offline with throwaway local git repos (no network/root/systemd): up-to-date no-op, update applies and runs the stub installer, dry run changes nothing, wrong branch refuses, local edits discarded by `reset --hard`, lock contention skips cleanly. The real `install.sh` was exercised end-to-end in this sandbox with stub `apt-get`/`systemctl` under `sudo -n`: all 5 steps ran, the generated `/etc/cron.d/econ-update` was byte-checked (valid cron.d syntax with the user field), perms `644`/`755` confirmed; sandbox artefacts were removed afterwards. Python suite re-run: **79/79**.
- **Not done / owner action:** nothing on `192.168.0.149` changed. The cron entry only appears after the owner runs `cd /opt/econ && git pull && ./install.sh` once. PR #11 (7-passer list + cadence) is tracked by `main`, so merging it is what pushes those changes out automatically ~30 min later.
- **Risks recorded (§7):** `main` is now a root-level production deploy path (guard GitHub write access/2FA); a bad `main` commit can take the dashboard down with no auto-rollback (owner's choice — recovery steps written into §8.4); hand edits on the server are wiped within 30 min.

### 2026-10-20 — Session 13: full passer list (3 → 7) + screen update-cadence check

- **Ask:** (a) stop limiting the Stocks tab to three names — show the full listing of the seven that passed the screen; (b) check whether this information is updated frequently enough; (c) update `handoff.md` and open a PR.
- **(a) Cap 3 → 7, every passer shown.** `dashboard/stocks.py`: `MAX_RESULTS` 3 → 7 (default display cap) with a new `MAX_RESULTS_CEILING = 32` hard clamp so a bad override cannot dump the whole universe; `limit` is now coerced with `int()` and clamped instead of `min(MAX_RESULTS, limit)`. `max_candidates` reports the *effective* cap. Message text now distinguishes "Showing all N stock(s) that passed" from "N passed … showing the top 7 by rank score", and `eligible_count` is always the full count. `dashboard/config.py`: `STOCK_MAX_RESULTS` is now `ECON_STOCK_MAX_RESULTS` (default 7). `dashboard/render.py`: the page no longer hard-slices to 3 — it renders every candidate up to the screen's `max_candidates`; header reads "every passer, up to 7 names"; the method panel gained a "Display" bullet stating that a longer list is a wider watchlist, not a stronger signal. **No filter, threshold, ranking weight, risk bucket, provider, or universe change.**
- **(b) Frequency — measured in this checkout, not assumed.** Board refresh cycle 300 s (`ECON_REFRESH`); the stock screen's own TTL is 3600 s (`TTL["stocks"]`), and `warm_all()` additionally skips the screen entirely while a *failed* refresh is younger than 1 h. Inputs are daily K-lines, so the values only change after the U.S. close (~04:00–05:00 MYT). Conclusion: the list is capped at one real update per trading day and re-checked hourly — polling keyless providers 12× more often would add load and no new information. Kept the 1 h TTL and made the cadence visible instead: `/stocks` prints "Refresh cadence: providers re-checked at most every 60 min · next re-check in about N min · the underlying daily bars only change after the U.S. close, so this list is not intraday", and `/api/stocks` + `/api/summary.stock_screen` expose `refresh_interval_seconds`, `checked_at_utc`, `next_check_in_seconds` (added by `app._with_stock_refresh_meta`). Recorded as **D20**.
- **Verification (offline only — the sandbox has no egress to market providers):** `python3 -m py_compile dashboard/*.py tests/*.py` clean; `python3 -m unittest discover -s tests -v` **79/79 passed** (was 74; 5 added/updated: every-passer ranking, 9-passer cap + clamp, page renders 5/7 cards, cap honoured when lowered, and 3 cadence tests). Demo server on `:8091`: `/`, `/summary`, `/stocks`, `/api/summary`, `/api/stocks`, `/healthz` all 200. Demo screen reported **12 eligible → 7 shown** (`max_candidates` 7, message "12 stock(s) passed … top 7"), 7 `stock-card` elements rendered, cadence line present. Demo data is synthetic — it exercises layout/API shape only and is not evidence about live passers.
- **Not done:** no LXC deploy (`git reset --hard origin/main` on `192.168.0.149` remains owner-gated, §5/§8), no live provider probe, no change to the 1 h TTL.

### 2026-10-13 — Session 12: Moomoo Malaysia usability, timestamps, and feed diagnostics

- **Ask:** continue the merged/deployed Stocks tab without changing its U.S.-listed under-$50 scope, gates, ranking, or three-total cap; check official Moomoo MY documentation; keep it read-only and do not deploy.
- **Moomoo docs checked:** [U.S. fractional-share guide](https://www.moomoo.com/my/support/topic9_193) says only certain U.S. stocks/ETFs are eligible and the app's list can change; [U.S. fee schedule](https://www.moomoo.com/my/support/topic9_136) lists broker, settlement/regulatory, and Malaysia stamp-duty items. No per-symbol Moomoo availability was inferred or claimed.
- **Implementation:** Stocks page now displays Kuala Lumpur (MYT) and U.S. Eastern timestamps; daily-bar dates are labelled as U.S. market session dates. It links the Moomoo guides, explains that availability is not queried, and shows a non-executable MYR share-price estimate only while the existing ECB/Frankfurter USD/MYR reference is fresh (reference date + fetch time). Fees, spreads, stamp duty and FX conversion charges are explicitly excluded. Synthetic demo FX conversions are suppressed.
- **Freshness/diagnostics:** coverage now counts histories with the minimum 61-session metrics available. Partial unresolved-ticker errors and relevant provider breaker states appear on `/stocks` and in `stock_screen`; existing stale-pick suppression remains. No changes to ticker universe, price, dollar-volume, freshness, trend, risk/ranking rules, providers, or trading integration.
- **Verification:** `python3 -m py_compile dashboard/*.py tests/*.py` passed; `python3 -m unittest discover -s tests -v` passed **70/70 tests**. No LXC deployment; owner-reported live baseline remains 32/32 history, Tencent K-line, seven passing, up to three displayed.

### 2026-10-13 — Session 11: Short-term stock watchlist tab

- **Ask:** add another tab for short-term stock research, classify High/Medium/Low risk, show no more than three stocks, and keep candidate share prices below $50.
- **UI/API:** added `/stocks`, `/api/stocks`, and `stock_screen` in `/api/summary`; all pages share the existing disk-backed cache. Output is capped at three US-listed names from an editable 32-symbol watchlist.
- **Screen:** daily data must be fresh (≤5 calendar days), price $5–<$50, average 20-session dollar volume ≥$5M, positive 20-session return, and close above its 20-day average. Ranking combines 5/20/60-session returns, a 50-day trend bonus, and realized-volatility/drawdown penalties. Risk labels use trailing 20-session annualized volatility and 60-session drawdown; thresholds and limitations are shown in the tab.
- **Providers:** Tencent adjusted daily K-lines with four-way concurrency → one Yahoo Finance spark batch for misses → Stooq `.us` history. The sandbox has no market-provider egress; Tencent's US K-line endpoint/exchange suffixes/volume units remain unverified on the LXC. Existing Yahoo 429 + Stooq timeout may leave the screen without live data; it returns no picks rather than using demo data.
- **Demo/tests:** demo backend generates synthetic bars with a visible DEMO-only banner. `py_compile` passed and `python3 -m unittest discover -s tests -v` passed **63 tests**. Demo HTTP checks for `/stocks`, `/api/stocks`, `/api/summary`, and `/healthz` returned 200; demo candidates are synthetic, not live.
- **Not done:** no LXC deploy/live provider validation. See §8 for exact verification commands.

### 2026-10-08 — Session 10: Summary tab

- **Inflation meter:** semicircle on `/summary`, needle on 10y breakeven (0–5%, green/amber/red). No new fetch.
- **Recession meter:** semicircle on `/summary`, needle on the 10y–2y spread (−1 to +1.5). Red inverted, amber flat, green positive. No new fetch.

- **Fix:** summary Commodities said "Nothing loaded" even when the board had cards. Formatter now keeps a quote with a missing close as "awaiting data" and no longer drops the row. Same cache as the board.

- **Ask:** another tab that summarizes the data already gathered.
- **Wiring:** `GET /summary` renders `build_summary_page` from the same `summary()` cache as `/`. Header tabs on both pages. Sections: posture, briefing lines, rates and inflation, indices, commodities, FX, CPI, GDP, coverage. No new upstreams. Server version `econ/0.9`.
- **Pull:** `cd /opt/econ && git fetch --all && git reset --hard origin/main && ./install.sh`, then open `http://192.168.0.149:8080/summary`.

### 2026-10-08 — Session 9: Real yield as the gold inflation indicator
 Real yield as the gold inflation indicator

- **Ask:** an inflation indicator to help a gold-heavy book. Not a price forecast.
- **Choice:** FRED `DFII10` (10-year TIPS real yield), the opportunity cost of holding gold.
  Breakeven (`T10YIE`, already on the board) is expected inflation only. A Gold read card
  uses DFII10 when loaded, otherwise Fed funds − breakeven: ≥1.5 headwind, 0.5–1.5 caution,
  negative = classic tailwind. Chart sits under breakeven. Copy says it is not a forecast.
- **Wiring:** `config.FRED_REAL`, cache key `fred:real`, `/api/summary` fields `real_yield`
  and `real_yield_meta`, briefing card `gold`. Server version `econ/0.8`.
- **Not done:** not probed on the LXC. Same FRED path as `T10YIE`, which was green at 02:09 UTC.

### 2026-10-08 — Session 8: Tencent/Sina fallback for the five empty index cards
 Tencent/Sina fallback for the five empty index cards

- **Ask:** the five "awaiting data" index cards are expected (no FRED series; Yahoo 429,
  CNBC 500, Stooq timeout). Probe alternatives, then wire a working one and update this file.
- **Probe (this environment, 2026-10-08 02:01 UTC, not the LXC):**
  - Tencent `qt.gtimg.cn` HTTP 200. `sh000001` 3851.62 at 10:01 +08, `hkHSI` 24090.14 at
    09:46, `ukUKX` 10458.50 at 2026-10-07 16:35. No symbol found for DAX or Euro Stoxx 50
    (`b_DAX`, `deDAX`, `euSX5E`, and others returned `v_pv_none_match`).
  - Sina `hq.sinajs.cn` HTTP 200. `s_sh000001` live; `int_hangseng` 24130.50 with a zero
    change; `b_UKX` 10458.50; `b_DAX` 25104.36 at 2026-10-07 23:30; `b_SX5E` 6180.30 at
    2026-10-08 00:00. `int_dax` / `int_fsx5e` / `int_eurstx50` empty. `int_ftse` 9284.83
    disagrees with the 10458.50 print — not used.
  - Eastmoney `push2delay` HTTP 502.
- **Wiring:** `ASIA_QUOTE_MAP` in `sources.py`. One Tencent batch and one Sina batch,
  10 min cache, own circuit breakers. Tencent wins when both answer. Two-point history so
  the existing spark/change card works. Cascade order unchanged for FRED-backed cards.
  `--doctor` §[8/8]. `tests/test_asia_quotes.py`. Server version `econ/0.7`.
- **Also this session:** confirmed PR #7 already on `main` (`c2dc6b9`); deleted the seven
  merged `arena/*` branches. Only `main` remains.
- **Live verdict (2026-10-08T02:09:21+00:00, `root@econ`):** owner pulled `5fd45e6` and ran
  `--doctor`. §[8/8] 5/5. Levels as in §6. Yahoo crumb/charts/spark still HTTP 429, CNBC
  still HTTP 500, Stooq still 5 s timeouts. Rates fallbacks recovered to 8/8 (NY Fed EFFR
  and ECB DFR green again). No further index work unless a card stays empty after the
  service warm (install restarted the unit).

### 2026-10-08 — Session 7: Investment briefing strip (v0.6)
 Investment briefing strip (v0.6)

- **Ask:** additional improvements or a summary on the dashboard to help with investments,
  then update this handoff.
- **Choice:** do not add upstreams (Yahoo still IP-429, CNBC still HTTP 500, Stooq dead).
  The useful gap was interpretation, not another quote. New `dashboard/briefing.py` reads
  the summary dict already assembled in `app.summary()`:
  - curve: inverted / flat (<0.50) / positive
  - policy: Fed funds, ECB deposit, real-rate proxy = funds − 10y breakeven
    (≥1.5 restrictive, <0 easy)
  - inflation: US CPI row if present, plus breakeven
  - growth: contracting economies and sub-1% real GDP
  - markets: populated index breadth, Brent/WTI and gold day-change, USD-cross direction,
    and a note when index cards are still awaiting data
- **Posture rule:** Defensive if the curve is inverted (or two risk flags); Cautious if any
  risk flag or two caution flags; Constructive otherwise; Awaiting data if every card is muted.
  Copy states it is a read of this board, not a forecast and not advice.
- **Wiring:** strip under the header in `render.py` (dark cards, tone colors already used by
  the board). `/api/summary` gains `briefing`. `Handler.server_version` bumped to `econ/0.6`.
- **Tests:** `tests/test_briefing.py` — inverted → Defensive, healthy → Constructive, empty
  board does not crash, page contains the strip. Offline, stdlib unittest.
- **Not done:** no deploy (branch only until merged). Index trim vs CNBC-500 investigation
  unchanged — still the owner's call in step 1 below.

### 2026-10-08 — Session 6: Post-merge (PR #5) live verdict — Yahoo IP-banned, CNBC 500, docs-only handoff update
 Post-merge (PR #5) live verdict — Yahoo IP-banned, CNBC 500, docs-only handoff update

- **Input:** owner merged PR #5 to `main` (`e22471c`), deployed on `root@econ:/opt/econ`
  (`git fetch && git reset --hard origin/main && ./install.sh`), and ran
  `python3 /opt/econ/dashboard/sources.py --doctor` at `2026-10-08T01:13:43+00:00`.
  The output contains the v0.5 probes (`crumb handshake`, §[6/7] CNBC) so the deploy is confirmed.
  Section-by-section observations vs the 00:52 (PR #4) run:
  - **[1/7] FX & macro 4/4** — stable. Frankfurter `.app` 1.08 s / `.dev` 0.33 s (-> 2026-10-07);
    World Bank CPI 0.15 s (-> 2.95), GDP 0.13 s (-> 2.16). Same values, normal latency jitter.
  - **[2/7] Rates 6/8 (was 8/8)** — FRED primaries all green in 0.13–0.51 s
    (`DFF` 3.88 @ 10-06, `ECBDFR` 2.5 @ 10-07, `T10Y2Y` 0.51, `T10YIE` 2.36) and both Treasury
    fallbacks green (10y-2y 1.77 s, breakeven 2.91 s). **New: `nyfed EFFR` SSL connection timeout
    (5.01 s) + `ecb DFR` 10.01 s timeout.** Fallback-only impact (board still 100% via FRED), but
    rates redundancy is degraded until re-probed — possibly transient.
  - **[3/7] FRED quotes 11/11** — byte-identical story to 00:52: daily Brent 125.44 (2d old),
    US/Nikkei 1–2d old, monthly IMF set 99d old accepted by the 120-day gate. No data drift.
  - **[4/7] FRED candidates** — `(no unverified candidates)` as expected; everything confirmed is wired.
  - **[5/7] Yahoo — IP-level 429 proven.** Cookie set (1.23 s, slower than 0.16 s = jitter), but
    **`crumb handshake` fails with HTTP 429 in 0.23 s on `query2`**, and all charts
    (`^GSPC` 0.35 s, `^GDAXI` 1.64 s, `^HSI` 1.48 s) + spark batch (1.18 s) are 429.
    The crumb *endpoint* rejecting the IP rules out any auth/pacing fix from this address.
    Breaker correctly cooling (`retry_in=879s` ≈ 15-min 429 policy). The 1.5 s pacing +
    cookie+crumb pipeline is implemented correctly — the network identity is what's blocked.
  - **[6/7] CNBC 0/6 — all HTTP 500 (not the predicted 403).** `.GDAXI` 2.58 s, `.FTSE` 0.15 s,
    `.HSI` 0.11 s, `.SSEC` 0.12 s, `.STOXX50E` 0.09 s, batch 1.18 s. Fast 500s = request reaches
    CNBC, server errors. Most likely symbol-format/param rejection or endpoint drift
    (`quote-html-webservice/quote.htm?...&requestMethod=quick`), less likely headers/IP block.
    Doctor does not log the 500 body today, so root cause needs the manual curls in §8.2.
    Breaker: `cnbc: degraded retry_in=0s`.
  - **[7/7] Stooq 0/3 + CoinGecko 2/2** — Stooq still 5.01 s connect timeouts ×3 (dead, ~0 ms in
    normal warming via breakers). CoinGecko fresh: PAX Gold 4133.21, Kinesis Silver 60.79
    (2026-10-08, <0.5 s) — small upticks vs 00:52, proving daily quotes flow.
- **Changes in this session (`arena/cda0bd47-econ`): docs-only, no code touched.**
  `handoff.md`: §2 branch → `arena/cda0bd47-econ`, §4 → v0.5-deployed, new D13 verdict row,
  §6 rewritten with the 01:13 measurements + unchanged board coverage (indices still 4/9:
  `^stx`/`^dax`/`^ukx`/`^shc`/`^hsi` empty; everything else 100%), §7 risks updated
  (Yahoo settled as IP ban; CNBC 500 suspects ordered; NY Fed/ECB flagged transient-or-redundant),
  §8 replaced with the owner decision (trim-to-4 / keep-9 / investigate-CNBC) + the two curl
  commands that unblock diagnosis + cooldown re-probe + summary check.
- **What this rules in/out:** Yahoo needs no further code iteration from this IP (any retry logic
  already exists and behaves correctly). CNBC is the only remaining cheap path to 9/9 indices,
  and only if the 500 body shows a fixable request problem. FRED/Treasury/CoinGecko/Frankfurter/
  World Bank are a verified-solid core — no changes proposed there.
- **Still needs owner/LXC:** the §8 decision + (for option C) the two CNBC curl bodies + a
  ≥30-min-cooldown `--doctor` re-run of §§[2/7]+[5/7] to classify Yahoo/NYFed/ECB as
  transient vs permanent.

### 2026-10-08 — Session 5: Live doctor verification of PR #4, Yahoo crumb auth, CNBC index fallback

- **Input:** owner deployed PR #4 (`e17d018`) to `root@econ:/opt/econ` and ran
  `python3 /opt/econ/dashboard/sources.py --doctor` at `2026-10-08T00:52:13+00:00`.
  Measured ground truth from the LXC:
  - Frankfurter `.app`/`.dev`: 1.34 / 0.16 s; World Bank CPI/GDP: 0.14 / 0.14 s (4/4 passed).
  - Rates & curve: 8/8 passed! `ustreasury breakeven` PASSED in 3.55 s -> ('2026-10-07', 2.36),
    verifying the case-insensitive header fix live.
  - FRED quote series: 11/11 passed (0.11–0.55 s). WTI monthly `POILWTIUSDM` (0.51 s) and Brent
    daily `DCOILBRENTEU` (0.11 s) + monthly `POILBREUSDM` (0.43 s), copper, wheat, aluminum, nickel
    all accepted by the 120-day IMF gate; daily indices fresh (1–2d old).
  - Yahoo Finance: cookie set in 0.16 s. But `chart ^GSPC` (0.17 s), `chart ^GDAXI` (1.50 s),
    `chart ^HSI` (1.48 s), and `spark batch` (1.50 s) all failed with `HTTP 429`. Conclusively proved
    that cookie alone does not resolve Yahoo's 429 on the LXC.
  - Stooq: connect timeouts bounded to 5.02 s each (15 s total probe vs old 63 s).
    CoinGecko `pax-gold` (0.46 s -> 4123.7) and `kinesis-silver` (0.48 s -> 60.2) passed.
  - Live board status: Commodities 8/8 (100%), FX 8/8 (100%), CPI 7/7 (100%), GDP 7/7 (100%),
    Rates & Curve 100%. Indices: 4/9 populated, 5/9 unpopulated (`^stx`, `^dax`, `^ukx`, `^shc`, `^hsi`).
- **Implemented on `arena/3caf56b5-econ` (v0.5 follow-up):**
  - **Yahoo Crumb Authentication:**
    * Modern Yahoo query APIs require a session crumb (`v1/test/getcrumb`) matching the `fc.yahoo.com`
      cookie to authorize requests.
    * Implemented `_fetch_yahoo_crumb(cookie)` and `_yahoo_session_crumb()` with 1-hour caching.
    * Appended `&crumb={crumb}` to `_yahoo_history` and `_yahoo_spark_batch` requests.
    * Added `crumb handshake` probe in `_doctor_yahoo`.
  - **CNBC Quote Provider & Cascade:**
    * Implemented `CNBC_MAP` (`.SPX`, `.NDX`, `.DJI`, `.STOXX50E`, `.GDAXI`, `.FTSE`, `.N225`,
      `.SSEC`, `.HSI`).
    * Implemented `_parse_cnbc_quotes`, `_cnbc_quote_to_history`, `_cnbc_batch`, and `_cnbc_history`
      with 10-minute caching. In warming, all 5 unmapped indices resolve in 1 single HTTP request.
    * Added CNBC to the cascade in `stooq_history`: `Yahoo -> FRED -> CNBC -> Stooq -> CoinGecko`.
    * Added Section `[6/7]` to `sources.py --doctor` to independently probe CNBC for `.GDAXI`,
      `.FTSE`, `.HSI`, `.SSEC`, and `.STOXX50E`, plus batch fetch.
  - **Transport & Simulation:**
    * Updated `tools/sim_lxc_network.py` to support CNBC simulation. Baseline `warm_all()`:
      **6.6 s / 24 of 24 keys filled** (indices 9/9, commodities 8/8, FX 8/8, CPI/GDP 7/7).
      When simulating CNBC 403 failure (`ECON_SIM_CNBC_FAIL=1`), gracefully completes in 6.8 s
      with 19/24 keys filled and breaker tripped.
  - **Verification — offline:**
    * `python3 -m unittest discover -s tests -v` → **47 passed** (all 37 existing + 10 new tests).
    * `python3 dashboard/sources.py --doctor` exits 0 with all 7 probe sections.
    * Demo mode on port 8080: summary has 9 indices and 8 commodities; HTML renders 29 inline SVGs.
- **Still needs LXC evidence:** rerun `python3 /opt/econ/dashboard/sources.py --doctor` after
  deploying PR #5 to verify whether Yahoo crumb handshake succeeds or if CNBC answers. Follow §8.

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
- **PR #4** (`arena/8d320fd8-econ` → `main`) is open for owner review/merge; it has not been merged
  or deployed. The agent stops before merging.

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
