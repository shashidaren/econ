#!/usr/bin/env bash
# Remove the econ dashboard service from the LXC (repo files are left in place).
set -euo pipefail
systemctl disable --now econ-dashboard.service 2>/dev/null || true
rm -f /etc/systemd/system/econ-dashboard.service
systemctl daemon-reload
echo "econ-dashboard removed. (App files remain in the repo checkout.)"
