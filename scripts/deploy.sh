#!/usr/bin/env bash
# Run on the Oracle VM to install or update eve-ice-monitor.
# Safe to re-run after any code change.
set -euo pipefail

APP_DIR="/opt/ice-monitor"
DATA_DIR="/var/lib/ice-monitor"
CONF_DIR="/etc/ice-monitor"
SERVICE_USER="icebot"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== EVE Ice Monitor Deploy ==="

# Create system user (no login shell, no home dir)
if ! id "$SERVICE_USER" &>/dev/null; then
    sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "Created user: $SERVICE_USER"
fi

# Create directories
sudo mkdir -p "$APP_DIR" "$DATA_DIR" "$CONF_DIR"
sudo chown "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
sudo chmod 750 "$DATA_DIR"

# Sync repo files (excludes secrets, state, and dev artifacts)
sudo rsync -a --delete \
    --exclude='.git' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.env*' \
    --exclude='esi_tokens.json' \
    --exclude='ice_monitor_state.json' \
    "$REPO_DIR/" "$APP_DIR/"
sudo chown -R root:root "$APP_DIR"

# Create or update virtualenv and install package
if [ ! -f "$APP_DIR/venv/bin/python" ]; then
    sudo python3 -m venv "$APP_DIR/venv"
fi
sudo "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
sudo "$APP_DIR/venv/bin/pip" install --quiet "$APP_DIR"
echo "Installed: ice-monitor $($APP_DIR/venv/bin/ice-monitor --help 2>&1 | head -1)"

# Install systemd units
sudo cp "$APP_DIR/ice-monitor.service" /etc/systemd/system/
sudo cp "$APP_DIR/ice-monitor-bot.service" /etc/systemd/system/
sudo systemctl daemon-reload

# Guard: env file must exist before enabling services
if [ ! -f "$CONF_DIR/env" ]; then
    echo ""
    echo "ACTION REQUIRED: /etc/ice-monitor/env does not exist."
    echo "Create it from the template, fill in your secrets, then re-run this script:"
    echo ""
    echo "  sudo cp $APP_DIR/.env.oracle.example $CONF_DIR/env"
    echo "  sudo nano $CONF_DIR/env"
    echo "  sudo chmod 600 $CONF_DIR/env && sudo chown root:root $CONF_DIR/env"
    echo ""
    exit 0
fi

# Enable and restart both services
sudo systemctl enable ice-monitor ice-monitor-bot
sudo systemctl restart ice-monitor ice-monitor-bot

echo ""
echo "=== Deployed successfully ==="
sudo systemctl status ice-monitor --no-pager -l | tail -5
sudo systemctl status ice-monitor-bot --no-pager -l | tail -5
echo ""
echo "Watch logs:  journalctl -u ice-monitor -u ice-monitor-bot -f"
