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
| Working branch | `arena/4b243469-econ` (session branch; merge to `main` when stable) |
| Deploy model | server does `git pull` + idempotent `install.sh` |
| App dir on server | `/opt/econ` |
| Port | **8080** (override: `ECON_PORT=xxxx ./install.sh`) |
| Service | `systemd` unit `econ-dashboard.service` |
| Runtime deps | **python3 only** — app is stdlib-pure (no pip, no venv, no Docker) |

## 3. Decisions

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Dashboard approach | **Custom dashboard app** (code in this repo) | Fully ours, glanceable wall-board, most repo-friendly |
| D2 | Install style | **Native** (apt python3 + systemd) | Lightest footprint in LXC; zero pip/venv/Docker |
| D3 | Data sources | **No-key only** | Stooq (quotes), Frankfurter/ECB (FX), World Bank (macro), FRED *public* `fredgraph.csv` (rates/curve — no API key needed, works anonymously). A free FRED API key can be added later (slot reserved in `.env.example`) |

Data-source rules: free, no signups; every source fails independently (panel shows
"awaiting data"/stale badge, board never breaks); polite fetch cadence via cache TTLs.

## 4. Architecture (v0.1, built)

```
dashboard/
  app.py        HTTP server (stdlib ThreadingHTTPServer) + cache + refresher thread
  config.py     ALL panels/symbols/TTLs — edit this to change the board
  sources.py    fetchers: stooq_history, fx_latest/series, worldbank_indicator, fred_series
  demo.py       same signatures as sources.py → synthetic data (ECON_DEMO=1)
  charts.py     server-side SVG: sparkline(), line_chart()
  render.py     single-page HTML/CSS builder (dark theme, auto-refresh every 5 min)
systemd/econ-dashboard.service
install.sh      idempotent server bootstrap (apt python3 → unit → enable → restart)
uninstall.sh
.env.example    optional overrides + reserved FRED key slot
```

- `/` board · `/api/summary` JSON · `/healthz` probe
- Background thread warms the cache every `ECON_REFRESH` (default 300 s); first load
  fills progressively ("awaiting data…" placeholders)
- Failed fetch keeps last good data and surfaces a ⚠ source-issue line in the footer
- Charts are inline SVG — **no CDN, works fully offline on LAN** once data is cached

## 5. Ops runbook (on the server)

```bash
# First install
apt install -y git
git clone https://github.com/shashidaren/econ /opt/econ
cd /opt/econ && ./install.sh

# Every later deploy (this is the whole workflow)
cd /opt/econ && git pull && ./install.sh

# Useful
journalctl -u econ-dashboard -f        # logs
systemctl status econ-dashboard
curl -s localhost:8080/healthz
cd /opt/econ && ./uninstall.sh         # remove service
```

Preview (no network needed): `ECON_DEMO=1 python3 dashboard/app.py` → sample data.

## 6. Current status

- [x] Repo + `handoff.md` convention
- [x] D1–D3 decided
- [x] App code (v0.1) — all panels, demo mode, error-stale handling
- [x] `install.sh` + systemd unit + uninstall
- [x] Tested in sandbox demo mode (27 SVGs, API OK, all sections render)
- [ ] **First real deploy on `192.168.0.149`** ← next action
- [ ] Verify live data on server (check stooq symbols `^stx`, `cb.f` render; edit
      `config.py` if a symbol is dead — panels fail gracefully meanwhile)
- [ ] Merge `arena/4b243469-econ` → `main` after first successful deploy

## 7. Known risks / watch-list

- Stooq is unofficial for some symbols; if `^stx` (Euro Stoxx 50) or `cb.f` (Brent)
  returns nothing, swap symbols in `dashboard/config.py` (e.g. Brent → `cb.f`, alt: use
  `oil` benchmarks available that day). Boards degrade per-panel, never crash.
- FRED `fredgraph.csv` is a public endpoint (no key) — if it ever starts requiring auth,
  register the free API key and switch `sources.fred_series` to the official API.
- World Bank data is annual (≈2-year lag) — that's inherent, not a bug.

## 8. Next steps (backlog)

1. Deploy to server, eyeball live values vs reality.
2. Ideas pool: global shipping (BDI) panel · debt-to-GDP panel · central-bank meeting
   calendar · multi-region yield curves · "snapshots" (PNG export for wall display) ·
   auto-merge to `main` via PR once stable.

## 9. Session log

### 2026-10-07 — Session 1: kickoff + v0.1 built
- Inspected repo: empty (`README.md` only) at commit `8e74b84`.
- Server confirmed: Debian LXC `econ` at `192.168.0.149`, root access.
- Established this `handoff.md` convention.
- **Decisions locked (D1–D3):** custom app, native install, no-key sources.
- **Built v0.1 of the dashboard:** stdlib-only Python app (see §4), systemd unit,
  idempotent `install.sh`/`uninstall.sh`, demo mode for offline previews.
- Tested in sandbox: all 7 sections render, `/api/summary` OK, graceful per-source
  error handling in place. 27 inline SVG charts, dark wall-board UI, 5-min auto-refresh.
- **Pending:** first deploy on the LXC (runbook in §5), then verify stooq symbols live.
