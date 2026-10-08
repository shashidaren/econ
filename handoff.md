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
| Working branch | `arena/cda0bd47-econ` (this session; PR #5 merged to `main` at `e22471c` on 2026-10-08; docs-only handoff update, no code changes) |
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
| D11 | Yahoo crumb authentication | **Fetch session crumb via `v1/test/getcrumb` using the session cookie and pass `&crumb={crumb}` to chart & spark endpoints** | Live LXC testing proved cookie alone still returns HTTP 429 across all endpoints; modern Yahoo query APIs require a crumb matching the session cookie to authorize requests |
| D12 | CNBC fallback for international indices | **Cascade across `Yahoo -> FRED -> CNBC -> Stooq -> CoinGecko` with 1-request batch caching for unmapped indices** | Euro Stoxx 50, DAX, FTSE 100, Shanghai Composite, and Hang Seng lack FRED coverage. When Yahoo fails, CNBC's key-less quote API provides live prices/changes in a single HTTP call; `--doctor` probes each independently |
| D13 | Post-merge live verdict (2026-10-08 01:13 UTC) | **Yahoo = IP-level HTTP 429 (crumb endpoint itself 429); CNBC = HTTP 500 on all 6 probes; board stays at 4/9 indices** | Both v0.5 hypotheses falsified live on `root@econ`. FRED + Treasury + CoinGecko + Frankfurter + World Bank carry 100% of populated cards. Owner to decide: trim `INDICES` to 4 verified cards vs keep 9 with "awaiting data" vs investigate CNBC 500 root cause / alternative key-less feeds |

Data-source rules: free, no signups; every source fails independently (panel shows
"awaiting data"/stale badge, board never breaks); polite fetch cadence via cache TTLs.

## 4. Architecture (v0.5 deployed; PR #5 merged at `e22471c`)

```
dashboard/
  app.py        HTTP server (ThreadingHTTPServer) + disk-backed cache + 4-worker refresher
                + pre-flight step: warm_providers() then prefetch_yahoo() before the fan-out
  config.py     ALL panels/symbols/TTLs/FRED_QUOTES/CNBC_MAP — edit this to change the board
  sources.py    multi-source fetchers + bounded curl/urllib transport + provider breakers
                + Yahoo cookie & crumb flow, CNBC batch quote provider,
                + 24 h FRED 404 cache, 5 y retry limit, Treasury header normalization,
                + CLI diagnostics (`python3 sources.py` / `--doctor` with 7 probe sections)
  demo.py       same signatures as sources.py → synthetic data (ECON_DEMO=1)
  charts.py     server-side SVG: sparkline(), line_chart()
  render.py     single-page HTML/CSS builder (dark theme, source & stale badges,
                footer "source issues" + "upstream health" lines, auto-refresh)
tests/test_sources.py    47 stdlib unittests — cascade, breakers, FRED gate/cache, transport,
                         Yahoo cookie+crumb, CNBC parsing & caching, Treasury headers, rendering
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
- [x] This session (`arena/cda0bd47-econ`): **docs-only** — no code changes. Handoff updated with
  the post-merge verdict; D13 recorded; §7/§8 rewritten around the trim-vs-investigate decision.
- [ ] Owner decision required (§8): trim `INDICES` to the 4 verified FRED cards, keep 9 cards with
  "awaiting data", and/or authorize a CNBC-500 root-cause investigation + Yahoo-cooldown re-probe.

## 7. Known risks / watch-list

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
- **International indices still 4/9:** DAX, FTSE, Euro Stoxx 50, Shanghai, Hang Seng have no FRED
  coverage and all three key-less fallbacks (Yahoo 429, CNBC 500, Stooq timeout) fail live.
  Nothing renders for these 5 cards except "awaiting data" + footer health line.
- **NY Fed / ECB fallback timeouts (new, fallback-only):** `nyfed EFFR` SSL timeout (5.01 s) and
  `ecb DFR` 10 s timeout at 01:13 UTC, after passing at 00:52. FRED primaries + Treasury CSVs
  cover the board, so no user impact — but if persistent, the rates cascade loses redundancy.
  Could be transient congestion, IPv4/TLS path issues, or LXC upstream flakiness. Re-probe first.
- **Stooq verified dead from the deployed LXC:** 3× 5.01 s connect timeouts. Breakers + parallel
  3 s pre-flight keep it at ~0 ms during regular warming.
- **Solid core (do not touch):** FRED 11/11 quotes + 4/4 policy/curve primaries, Treasury 10y-2y +
  breakeven, Frankfurter `.app`/`.dev`, World Bank CPI/GDP, CoinGecko gold/silver — all green with
  sub-second to ~3 s latencies and fresh dates.
- **Sandbox has no egress to data hosts:** only GitHub/PyPI are reachable from this environment.
  All local tests use faked transports; live provider behavior must be verified on `root@econ`.

## 8. Next steps (owner decision + diagnostics)

1. **Decide the board shape** (pick one; all are one-line `config.py` changes if trimming):
   - **(A) Trim to 4 verified indices** (`^spx`, `^ndx`, `^dji`, `^nkx`) — clean board, zero empty
     cards, everything live off FRED. Recommended if the 5 internationals are nice-to-have.
   - **(B) Keep 9 cards** with "awaiting data" + footer health line — preserves layout while
     alternatives are investigated. Zero code change.
   - **(C) Authorize a CNBC-500 investigation** (agent work, needs 1–2 LXC curl outputs — step 2).
2. **Capture the CNBC 500 body + variants from the LXC** (this is what unblocks the diagnosis —
   today's doctor only logs the status code):
   ```bash
   # Exact URL the app requests (batch of 5), with body shown:
   curl -4 -sS -i --max-time 10 -A 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36' \
     'https://quote.cnbc.com/quote-html-webservice/quote.htm?noform=1&partnerId=2&fund=1&exthrs=0&output=json&symbolType=issue&symbols=.GDAXI%7C.FTSE%7C.HSI%7C.SSEC%7C.STOXX50E&requestMethod=quick' | head -c 2000
   # Single-symbol + alternate symbol spelling (tests the symbol-format hypothesis):
   curl -4 -sS -i --max-time 10 -A 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36' \
     'https://quote.cnbc.com/quote-html-webservice/quote.htm?noform=1&partnerId=2&fund=1&exthrs=0&output=json&symbolType=issue&symbols=.GDAXI&requestMethod=quick' | head -c 2000
   ```
   Paste both outputs back. If the body names a bad param/symbol, the fix is usually a small
   `_cnbc_batch` URL change + new unit test; if it is an abuse/geo block, we stop and pick (A)/(B).
3. **Re-probe transients** (Yahoo cooldown + NY Fed/ECB): wait ≥30 min after the last doctor run,
   then `python3 /opt/econ/dashboard/sources.py --doctor` once and paste §§[2/7] + [5/7] + breakers.
   - Yahoo 429 clearing → note the cooldown length; Yahoo stays opportunistic in the cascade.
   - NY Fed/ECB green again → close as transient; still red → rates redundancy is FRED+Treasury only.
4. **Confirm the served board:** `curl -s localhost:8080/api/summary` (expect indices 4/9,
   commodities 8/8, fx 8/8, cpi/gdp 7/7, curve + breakeven present, `providers` showing yahoo/cnbc
   state) and `journalctl -u econ-dashboard -n 30 --no-pager` for refresher errors.

## 9. Session log

### 2026-10-08 — Session 6: Post-merge (PR #5) live verdict — Yahoo IP-banned, CNBC 500, docs-only handoff update

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
