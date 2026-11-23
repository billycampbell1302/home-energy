#!/bin/bash
# Installs Home Energy on a fresh Ubuntu 22.04/24.04 server (Oracle Cloud Always Free).
# Run as root. Expects ADMIN_PASSWORD (and optionally PEOPLE) in the environment - see deploy/cloud-init.sh.
set -euxo pipefail
exec > >(tee -a /var/log/home-energy-install.log) 2>&1

REPO="${REPO:-https://github.com/billycampbell1302/home-energy.git}"
APP=/opt/home-energy
DATA=/var/lib/home-energy
ENVF=/etc/home-energy.env

timedatectl set-timezone Europe/London || true
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv python3-pip git curl caddy iptables-persistent

# Small servers (1 GB RAM) - add swap so installs don't run out of memory.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Oracle's Ubuntu images block everything except SSH in the server's own firewall. Open web ports.
iptables -I INPUT -p tcp -m multiport --dports 80,443 -m conntrack --ctstate NEW -j ACCEPT
netfilter-persistent save

id homeenergy &>/dev/null || useradd --system --home "$DATA" --shell /usr/sbin/nologin homeenergy
mkdir -p "$DATA" && chown homeenergy:homeenergy "$DATA" && chmod 700 "$DATA"

if [ ! -d "$APP/.git" ]; then git clone "$REPO" "$APP"; fi
git config --global --add safe.directory "$APP"
python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r "$APP/requirements.txt"

# Public address: a free HTTPS name based on the server's IP, e.g. 132-145-1-2.sslip.io
IP=$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me)
DOMAIN="${DOMAIN:-${IP//./-}.sslip.io}"

if [ ! -f "$ENVF" ]; then
  cat > "$ENVF" <<EOF
ADMIN_PASSWORD=${ADMIN_PASSWORD:-changeme}
SESSION_SECRET=$(openssl rand -hex 32)
PEOPLE=${PEOPLE:-Billy,Wife}
LAT=${LAT:-51.81}
LON=${LON:-0.75}
DATA_DIR=$DATA
PUBLIC_URL=https://$DOMAIN
EOF
  chmod 600 "$ENVF"
fi

cat > /etc/systemd/system/home-energy.service <<EOF
[Unit]
Description=Home Energy dashboard
After=network-online.target
Wants=network-online.target

[Service]
User=homeenergy
EnvironmentFile=$ENVF
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Auto-update: checks GitHub every 15 minutes and restarts if the code changed.
cat > /etc/systemd/system/home-energy-update.service <<EOF
[Unit]
Description=Update Home Energy from GitHub
[Service]
Type=oneshot
ExecStart=/bin/bash $APP/deploy/update.sh
EOF
cat > /etc/systemd/system/home-energy-update.timer <<EOF
[Unit]
Description=Check for Home Energy updates
[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
[Install]
WantedBy=timers.target
EOF

cat > /etc/caddy/Caddyfile <<EOF
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
EOF

systemctl daemon-reload
systemctl enable --now home-energy home-energy-update.timer
systemctl restart caddy
echo "DONE - open https://$DOMAIN"
