#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/stalzone-price-tracker"
BRANCH="master"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash $APP_DIR/deploy/oracle/update.sh"
  exit 1
fi

git -C "$APP_DIR" fetch origin "$BRANCH"
git -C "$APP_DIR" checkout "$BRANCH"
git -C "$APP_DIR" reset --hard "origin/$BRANCH"
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
chown -R stalzone:stalzone "$APP_DIR"
systemctl restart stalzone-tracker
sleep 3
systemctl --no-pager --full status stalzone-tracker
