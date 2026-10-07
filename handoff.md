# econ — Project Handoff

> **Convention:** this file is updated on *every* change session. Anyone (human or agent)
> should be able to resume work from this file alone. Newest entries go at the **top** of
> the Session Log (§8). Update §3 (Decisions), §5 (Status), §7 (Next steps) as things change.

---

## 1. Project goal

A **self-hosted, glanceable global macro dashboard** running on a Debian LXC container.
Target vibe: a simplified "world economy at a glance" wall-board — dark mode, big charts,
no day-trading noise. Core themes:

- Global stock indices (US, EU, Japan, China, HK)
- Commodities (oil, gold, copper, wheat)
- Currencies (USD strength, EUR, JPY, CNY)
- Inflation / CPI across major economies
- Central-bank policy rates
- US yield curve (10Y–2Y spread, recession indicator)

## 2. Environment

| Item | Value |
|---|---|
| Server | Debian LXC (Proxmox guest), hostname `econ` |
| Server IP | `192.168.0.149` (LAN only) |
| Access | root via SSH; web UI on LAN |
| Repo | `shashidaren/econ` (this repo) |
| Working branch | `arena/4b243469-econ` (session branch; merge to `main` when stable) |
| Deploy model | server does `git pull` + idempotent `install.sh` |

**Deployment workflow:**
1. Changes are made in this repo and committed.
2. On the server: `cd /opt/econ && git pull && sudo ./install.sh` (idempotent — safe to re-run).
3. Services run under systemd; no manual steps after install.

## 3. Decisions

| # | Decision | Options | Choice | Status |
|---|---|---|---|---|
| D1 | Dashboard approach | Custom app / Homepage+TradingView / Grafana+FRED / OpenBB | — | ⏳ pending |
| D2 | Install style on LXC | Native (apt + venv + systemd) / Docker Compose | — | ⏳ pending |
| D3 | Data sources | No-key only (World Bank, ECB FX, Stooq) / + free FRED API key | — | ⏳ pending |

Notes:
- FRED key is free (register at <https://fred.stlouisfed.org/docs/api/api_key.html>).
  If used, it lives in `/opt/econ/.env` on the server (**git-ignored — never commit secrets,
  never paste keys in chat**).

## 4. Candidate architectures (from prior discussion)

1. **Custom lightweight dashboard app** — small Python/Node service *in this repo*; dark
   single-page UI; fetches free macro APIs server-side with caching. Most repo-driven,
   fully customizable, single service + systemd.
2. **Homepage (gethomepage.dev) / Dashy + TradingView embeds** — zero coding, ~15 min,
   but layout limited to available widgets; configs (YAML) stored in repo.
3. **Grafana + FRED** — beautiful gauges/graphs; more setup + plugin maintenance.
4. **OpenBB** — deepest interactive macro tooling; heaviest install; not a wall-board.

## 5. Current status

- [x] Repo exists (`shashidaren/econ`), empty besides README
- [x] `handoff.md` convention established
- [ ] Stack decision (D1–D3)
- [ ] Server prep (packages)
- [ ] App code + configs in repo
- [ ] `install.sh` + systemd units
- [ ] First deploy to `192.168.0.149`
- [ ] Dashboard visible at `http://192.168.0.149:<port>`

## 6. Server prep — packages (draft, final after D1/D2)

```bash
# Always useful regardless of stack
apt update && apt full-upgrade -y
apt install -y git curl ca-certificates chrony

# If custom app (native):    apt install -y python3 python3-venv python3-pip
# If Homepage:               apt install -y nodejs npm   (or Docker)
# If Grafana:                add grafana apt repo, apt install -y grafana
# If Docker route:           apt install -y docker.io docker-compose-v2
```

## 7. Next steps

1. Confirm D1–D3 decisions (see Session Log).
2. Write `install.sh` (idempotent) + app/configs in repo.
3. Clone/pull repo on server at `/opt/econ`, run `install.sh`.
4. Verify dashboard on LAN, auto-start on boot (systemd), set update cadence/cache.
5. Update this file.

## 8. Session log

### 2026-10-07 — Session 1: kickoff
- Inspected repo: empty (`README.md` only) at commit `8e74b84`.
- Server confirmed: Debian LXC `econ` at `192.168.0.149`, root access.
- Established this `handoff.md` convention: update on every change session.
- Awaiting decisions D1 (approach), D2 (install style), D3 (data sources) before
  writing `install.sh` and app code.
