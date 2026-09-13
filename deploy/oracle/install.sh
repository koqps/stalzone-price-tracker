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

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/oracle/install.sh"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y git python3 python3-venv python3-pip nginx curl ca-certificates

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$APP_DIR"
else
  git -C "$APP_DIR" fetch origin "$BRANCH"
  git -C "$APP_DIR" checkout "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  echo
  echo "Enter secrets here. Input is hidden and is written only to $ENV_FILE."
  read -r -s -p "DISCORD_TOKEN: " DISCORD_TOKEN; echo
  read -r -p "SUPABASE_SYNC_URL: " SUPABASE_SYNC_URL
  read -r -s -p "SUPABASE_SYNC_SECRET: " SUPABASE_SYNC_SECRET; echo
  read -r -p "DISCORD_ALERT_WEBHOOK (optional; press Enter to skip): " DISCORD_ALERT_WEBHOOK
  read -r -p "CHANNEL_ID (optional; press Enter to skip): " CHANNEL_ID
  cat > "$ENV_FILE" <<EOF
DISCORD_TOKEN=$DISCORD_TOKEN
SUPABASE_SYNC_URL=$SUPABASE_SYNC_URL
SUPABASE_SYNC_SECRET=$SUPABASE_SYNC_SECRET
DISCORD_ALERT_WEBHOOK=$DISCORD_ALERT_WEBHOOK
CHANNEL_ID=$CHANNEL_ID
LIVE_MARKET_DATA=true
REGION=na
REALM=global
PORT=$PORT
PYTHONUNBUFFERED=1
EOF
  chmod 600 "$ENV_FILE"
fi

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
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=$APP_DIR/cache

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$APP_DIR/cache"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/cache"

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
        proxy_read_timeout 120s;
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
systemctl --no-pager --full status stalzone-tracker || true
curl -fsS "http://127.0.0.1:$PORT/health" || curl -fsS "http://127.0.0.1:$PORT/" >/dev/null

echo
echo "StalZone tracker installed."
echo "Service: systemctl status stalzone-tracker"
echo "Logs:    journalctl -u stalzone-tracker -f"
echo "Update:  sudo bash $APP_DIR/deploy/oracle/update.sh"
