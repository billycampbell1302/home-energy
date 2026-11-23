#!/bin/bash
# Pulls the latest code from GitHub and restarts the app if anything changed.
set -euo pipefail
cd /opt/home-energy
git fetch --quiet origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  old_req=$(sha1sum requirements.txt)
  git reset --hard --quiet origin/main
  if [ "$old_req" != "$(sha1sum requirements.txt)" ]; then
    .venv/bin/pip install --quiet -r requirements.txt
  fi
  systemctl restart home-energy
  echo "Updated to $(git rev-parse --short HEAD)"
fi
