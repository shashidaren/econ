#!/usr/bin/env bash
# econ dashboard — server installer (idempotent; safe to re-run to deploy updates)
#
# Usage on the Debian LXC (as root):
#   git clone https://github.com/shashidaren/econ /opt/econ   # first time only
#   cd /opt/econ && ./install.sh                              # install / update
#
# Optional overrides:
#   ECON_PORT=9000 ./install.sh                     # listen on another port (default 8080)
#   ECON_UPDATE_SCHEDULE='0 * * * *' ./install.sh   # auto-update hourly instead of every 30 min
#
# This installer also drops /etc/cron.d/econ-update, which runs
# tools/auto_update.sh on a schedule: it fast-forwards the checkout to
# origin/main and re-runs this installer when the commit changes. Edit the
# schedule in that file (or remove it) at any time.

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${ECON_PORT:-8080}"
UNIT_SRC="$APP_DIR/systemd/econ-dashboard.service"
UNIT_DST="/etc/systemd/system/econ-dashboard.service"
CRON_DST="/etc/cron.d/econ-update"
CRON_SCHEDULE="${ECON_UPDATE_SCHEDULE:-*/30 * * * *}"
UPDATE_LOG="/var/log/econ-update.log"
UPDATE_SCRIPT="$APP_DIR/tools/auto_update.sh"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root (ssh root@192.168.0.149)" >&2
  exit 1
fi

echo "==> [1/5] Installing system packages (python3 + curl — the app is stdlib-pure)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends python3 ca-certificates curl cron

echo "==> [2/5] Smoke test"
python3 -c "import sys; sys.path.insert(0, '$APP_DIR/dashboard'); import config, render, charts, sources, news, app; print('modules import OK')"

echo "==> [3/5] Installing systemd unit (port $PORT, dir $APP_DIR)"
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__PORT__|$PORT|g" \
    "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"
systemctl daemon-reload
systemctl enable econ-dashboard.service
systemctl restart econ-dashboard.service   # restart = picks up freshly pulled code

echo "==> [4/5] Installing unattended auto-update cron entry ($CRON_DST)"
if [ -f "$UPDATE_SCRIPT" ]; then
  chmod 755 "$UPDATE_SCRIPT"
  touch "$UPDATE_LOG"
  chmod 644 "$UPDATE_LOG"
  cat > "$CRON_DST" <<EOF
# econ dashboard — unattended updates (written by install.sh; edit freely).
# Tracks origin/main; restarts the service only when the commit changes.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$CRON_SCHEDULE root $UPDATE_SCRIPT >> $UPDATE_LOG 2>&1
EOF
  chmod 644 "$CRON_DST"
  echo "    schedule: $CRON_SCHEDULE   log: $UPDATE_LOG"
else
  echo "    skipped: $UPDATE_SCRIPT not found"
fi

echo "==> [5/5] Verifying"
sleep 2
if systemctl is-active --quiet econ-dashboard.service; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo ""
  echo "✔ econ-dashboard is running"
  echo "  Dashboard : http://${IP:-<server-ip>}:$PORT"
  echo "  Health    : http://${IP:-<server-ip>}:$PORT/healthz"
  echo ""
  echo "  logs      : journalctl -u econ-dashboard -f"
  echo "  auto-update: $CRON_DST ($CRON_SCHEDULE) — tail -f $UPDATE_LOG"
  echo "  manual    : cd $APP_DIR && git pull && ./install.sh"
  echo "  stop      : systemctl disable --now econ-dashboard"
  echo "  no auto   : rm -f $CRON_DST   (updates then stay manual)"
else
  echo "ERROR: service failed to start — recent logs:" >&2
  journalctl -u econ-dashboard -n 30 --no-pager >&2
  exit 1
fi
