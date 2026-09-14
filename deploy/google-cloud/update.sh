#!/usr/bin/env bash
set -euo pipefail
APP_DIR="/opt/stalzone-price-tracker"
REPO_URL="https://github.com/koqps/stalzone-price-tracker.git"
BRANCH="master"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/google-cloud/update.sh"
  exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "Tracker install not found at $APP_DIR"
  exit 1
fi

git config --global --add safe.directory "$APP_DIR" || true
git -C "$APP_DIR" fetch origin "$BRANCH"
git -C "$APP_DIR" reset --hard "origin/$BRANCH"
chown -R stalzone:stalzone "$APP_DIR"

systemctl daemon-reload
systemctl restart stalzone-tracker
systemctl reload nginx || true

# Startup can legitimately take over a minute while persistence hydrates.
# Wait up to 3 minutes before treating the deploy as unhealthy.
for _ in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:8420/health >/dev/null 2>&1; then
    echo "Tracker updated and healthy."
    curl -fsS http://127.0.0.1:8420/health
    echo
    exit 0
  fi
  sleep 2
done

echo "Tracker did not become healthy in time. Recent logs:"
journalctl -u stalzone-tracker -n 120 --no-pager
exit 1
