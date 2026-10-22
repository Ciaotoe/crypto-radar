#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

REPO_DIR="${REPO_DIR:-/opt/crypto-radar}"
SERVICE_USER="${SERVICE_USER:-crypto-radar}"
SERVICE_HOME="${SERVICE_HOME:-/home/crypto-radar}"
ENV_FILE="${ENV_FILE:-/etc/crypto-radar.env}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/crypto-radar}"

apt-get update
apt-get install -y ca-certificates curl git
timedatectl set-ntp true 2>/dev/null || true

if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
  groupadd --system "$SERVICE_USER"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SERVICE_USER" --create-home --home-dir "$SERVICE_HOME" \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
if [ ! -x /usr/local/bin/uv ]; then
  UV_BIN="$(command -v uv || true)"
  if [ -n "$UV_BIN" ]; then
    ln -sf "$UV_BIN" /usr/local/bin/uv
  fi
fi
if [ ! -x /usr/local/bin/uv ]; then
  echo "uv was not installed at /usr/local/bin/uv" >&2
  exit 1
fi

if [ ! -d "$REPO_DIR/.git" ]; then
  : "${REPO_URL:?set REPO_URL to clone the repository}"
  git clone "$REPO_URL" "$REPO_DIR"
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_DIR"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$REPO_DIR/data"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$BACKUP_DIR"

if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" <<'EOF'
TG_BOT_TOKEN=
TG_CHAT_ID=
GEMINI_API_KEY=
RADAR_HEARTBEAT=1
RADAR_DAILY_HEARTBEAT=1
TZ=Asia/Shanghai
EOF
  chmod 600 "$ENV_FILE"
  echo "created $ENV_FILE; fill in the secrets before starting the service"
fi
chmod 600 "$ENV_FILE"

runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" \
  /usr/local/bin/uv python install 3.12
runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" \
  /usr/local/bin/uv sync --frozen --directory "$REPO_DIR"

install -m 0644 "$REPO_DIR/deploy/crypto-radar.service" /etc/systemd/system/crypto-radar.service
install -m 0644 "$REPO_DIR/deploy/crypto-radar-backup.service" /etc/systemd/system/crypto-radar-backup.service
install -m 0644 "$REPO_DIR/deploy/crypto-radar-backup.timer" /etc/systemd/system/crypto-radar-backup.timer
install -m 0644 "$REPO_DIR/deploy/crypto-radar-daily.service" /etc/systemd/system/crypto-radar-daily.service
install -m 0644 "$REPO_DIR/deploy/crypto-radar-daily.timer" /etc/systemd/system/crypto-radar-daily.timer
install -m 0755 "$REPO_DIR/deploy/crypto-radar-loop.sh" /usr/local/sbin/crypto-radar-loop
install -m 0755 "$REPO_DIR/deploy/backup.sh" /usr/local/sbin/crypto-radar-backup

systemctl disable --now crypto-radar.timer 2>/dev/null || true
rm -f /etc/systemd/system/crypto-radar.timer

systemctl daemon-reload
systemctl enable crypto-radar.service crypto-radar-backup.timer crypto-radar-daily.timer
systemctl start crypto-radar-backup.timer crypto-radar-daily.timer

echo "installed. Fill $ENV_FILE, then run:"
echo "  systemctl start crypto-radar.service"
echo "  journalctl -u crypto-radar.service -f"
