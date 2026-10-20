#!/usr/bin/env bash
# Remove the econ dashboard service from the LXC (repo files are left in place).
set -euo pipefail
systemctl disable --now econ-dashboard.service 2>/dev/null || true
rm -f /etc/systemd/system/econ-dashboard.service
rm -f /etc/cron.d/econ-update          # stop unattended updates too
systemctl daemon-reload
echo "econ-dashboard removed (service + auto-update cron). App files and"
echo "/var/log/econ-update.log remain in the repo checkout."
