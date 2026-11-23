#!/bin/bash
# Pulls the latest code from GitHub and restarts the app if anything changed.
# Runs as root: every 15 minutes from a timer, and straight away when "Check for updates" is tapped on the dashboard.
set -euo pipefail
DATA=/var/lib/home-energy
STATUS="$DATA/update-status.json"
cd /opt/home-energy

# Make sure the dashboard's "Check for updates" button is wired up (added after the first install).
if [ ! -f /etc/systemd/system/home-energy-update.path ]; then
  cat > /etc/systemd/system/home-energy-update.path <<EOF
[Unit]
Description=Update Home Energy when requested from the dashboard
[Path]
PathChanged=$DATA/update-request
Unit=home-energy-update.service
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now home-energy-update.path
fi

write_status() {
  printf '{"checked": %s, "result": "%s", "commit": "%s"}\n' "$(date +%s)" "$1" "$(git rev-parse --short HEAD)" > "$STATUS"
  chown homeenergy:homeenergy "$STATUS" || true
}

if ! git fetch --quiet origin main; then
  write_status "Couldn't reach GitHub"
  exit 1
fi
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  old_req=$(sha1sum requirements.txt)
  git reset --hard --quiet origin/main
  if [ "$old_req" != "$(sha1sum requirements.txt)" ]; then
    .venv/bin/pip install --quiet -r requirements.txt
  fi
  write_status "Updated"
  systemctl restart home-energy
  echo "Updated to $(git rev-parse --short HEAD)"
else
  write_status "Already up to date"
fi
