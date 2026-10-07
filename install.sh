#!/usr/bin/env bash
# econ dashboard — server installer (idempotent; safe to re-run to deploy updates)
#
# Usage on the Debian LXC (as root):
#   git clone https://github.com/shashidaren/econ /opt/econ   # first time only
#   cd /opt/econ && ./install.sh                              # install / update
#
# Optional overrides:
#   ECON_PORT=9000 ./install.sh      # listen on another port (default 8080)

set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${ECON_PORT:-8080}"
UNIT_SRC="$APP_DIR/systemd/econ-dashboard.service"
UNIT_DST="/etc/systemd/system/econ-dashboard.service"

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root (ssh root@192.168.0.149)" >&2
  exit 1
fi

echo "==> [1/4] Installing system packages (python3 + curl — the app is stdlib-pure)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends python3 ca-certificates curl

echo "==> [2/4] Smoke test"
python3 -c "import sys; sys.path.insert(0, '$APP_DIR/dashboard'); import config, render, charts, sources, app; print('modules import OK')"

echo "==> [3/4] Installing systemd unit (port $PORT, dir $APP_DIR)"
sed -e "s|__APP_DIR__|$APP_DIR|g" -e "s|__PORT__|$PORT|g" \
    "$UNIT_SRC" > "$UNIT_DST"
chmod 644 "$UNIT_DST"
systemctl daemon-reload
systemctl enable econ-dashboard.service
systemctl restart econ-dashboard.service   # restart = picks up freshly pulled code

echo "==> [4/4] Verifying"
sleep 2
if systemctl is-active --quiet econ-dashboard.service; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo ""
  echo "✔ econ-dashboard is running"
  echo "  Dashboard : http://${IP:-<server-ip>}:$PORT"
  echo "  Health    : http://${IP:-<server-ip>}:$PORT/healthz"
  echo ""
  echo "  logs    : journalctl -u econ-dashboard -f"
  echo "  update  : cd $APP_DIR && git pull && ./install.sh"
  echo "  stop    : systemctl disable --now econ-dashboard"
else
  echo "ERROR: service failed to start — recent logs:" >&2
  journalctl -u econ-dashboard -n 30 --no-pager >&2
  exit 1
fi
