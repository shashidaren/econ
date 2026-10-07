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
apt install -y git
git clone https://github.com/shashidaren/econ /opt/econ
cd /opt/econ && ./install.sh
# → http://192.168.0.149:8080
```

Deploy updates any time:

```bash
cd /opt/econ && git pull && ./install.sh
```

Preview without a server (sample data): `ECON_DEMO=1 python3 dashboard/app.py`

## Data sources (all free, no keys)

| Source | Used for |
|---|---|
| Stooq | daily closes for indices & commodities |
| Frankfurter (ECB reference rates) | FX, USD base |
| World Bank API | annual inflation & GDP growth |
| FRED public CSV (`fredgraph.csv`) | policy rates, 10Y–2Y curve, breakeven inflation |

Not investment advice — it's a glance-board, not a trading terminal.
