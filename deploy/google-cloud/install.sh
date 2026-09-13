#!/usr/bin/env bash
set -euo pipefail
APP_USER="stalzone"
APP_DIR="/opt/stalzone-price-tracker"
ENV_FILE="/etc/stalzone-tracker.env"
SERVICE_FILE="/etc/systemd/system/stalzone-tracker.service"
NGINX_SITE="/etc/nginx/sites-available/stalzone-tracker"
REPO_URL="https://github.com/koqps/stalzone-price-tracker.git"
BRANCH="master"
PORT="8420"

if [[ $EUID -ne 0 ]]; then echo "Run with sudo"; exit 1; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git python3 python3-venv python3-pip nginx curl ca-certificates

# e2-micro has 1 GB RAM; add swap so Python builds and scans remain stable.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
# Git 2.35+ refuses repositories owned by another account unless explicitly trusted.
git config --global --add safe.directory "$APP_DIR" || true
if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Enter the existing tracker secrets from Render. Hidden values stay only on this VM."
  read -r -s -p "DISCORD_TOKEN: " DISCORD_TOKEN; echo
  read -r -p "EXBO_CLIENT_ID: " EXBO_CLIENT_ID
  read -r -s -p "EXBO_CLIENT_SECRET: " EXBO_CLIENT_SECRET; echo
  read -r -p "SUPABASE_SYNC_URL: " SUPABASE_SYNC_URL
  read -r -s -p "SUPABASE_TRACKER_SECRET: " SUPABASE_TRACKER_SECRET; echo
  read -r -p "CHANNEL_ID (optional): " CHANNEL_ID
  read -r -p "TARGET_GUILD_IDS (optional): " TARGET_GUILD_IDS
  cat > "$ENV_FILE" <<EOF
DISCORD_TOKEN=$DISCORD_TOKEN
EXBO_CLIENT_ID=$EXBO_CLIENT_ID
EXBO_CLIENT_SECRET=$EXBO_CLIENT_SECRET
SUPABASE_SYNC_URL=$SUPABASE_SYNC_URL
SUPABASE_TRACKER_SECRET=$SUPABASE_TRACKER_SECRET
CHANNEL_ID=$CHANNEL_ID
TARGET_GUILD_IDS=$TARGET_GUILD_IDS
LIVE_MARKET_DATA=false
REGION=na
REALM=global
PORT=$PORT
PYTHONUNBUFFERED=1
EOF
  chmod 600 "$ENV_FILE"
fi

mkdir -p "$APP_DIR/cache"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/cache"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=StalZone Market Tracker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python combined.py
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=$APP_DIR/cache

[Install]
WantedBy=multi-user.target
EOF

cat > "$NGINX_SITE" <<EOF
server {
  listen 80 default_server;
  listen [::]:80 default_server;
  server_name _;
  location / {
    proxy_pass http://127.0.0.1:$PORT;
    proxy_http_version 1.1;
    proxy_set_header Host \$host;
    proxy_set_header X-Real-IP \$remote_addr;
    proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto \$scheme;
  }
}
EOF
rm -f /etc/nginx/sites-enabled/default
ln -sf "$NGINX_SITE" /etc/nginx/sites-enabled/stalzone-tracker
nginx -t
systemctl enable --now nginx
systemctl daemon-reload
systemctl enable --now stalzone-tracker
sleep 3
curl -fsS "http://127.0.0.1:$PORT/health" || curl -fsS "http://127.0.0.1:$PORT/" >/dev/null
systemctl --no-pager status stalzone-tracker || true
echo "Install complete. LIVE_MARKET_DATA is false for migration safety."
echo "After verification, enable alerts by setting LIVE_MARKET_DATA=true in $ENV_FILE and restarting the service."
