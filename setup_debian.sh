#!/usr/bin/env bash
# Install Addicted as a systemd service on a fresh Debian/Ubuntu box.
# Listens on 0.0.0.0:8080 by default.
#
# Usage:  sudo ./setup_debian.sh <RGAPI-key> [PORT] [HOST]
#
# Examples:
#   sudo ./setup_debian.sh RGAPI-abcd-1234
#   sudo ./setup_debian.sh RGAPI-abcd-1234 9000
#   sudo ./setup_debian.sh RGAPI-abcd-1234 8080 0.0.0.0

set -euo pipefail

API_KEY="${1:-}"
PORT="${2:-8080}"
HOST="${3:-0.0.0.0}"

INSTALL_DIR="/opt/addicted"
SVC_USER="addicted"
SVC_NAME="addicted"
ENV_FILE="/etc/addicted/env"

# ---- Pre-flight ----
if [ -z "${API_KEY}" ]; then
    echo "Usage: sudo $0 <RGAPI-key> [PORT=8080] [HOST=0.0.0.0]" >&2
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo." >&2
    exit 1
fi

if [ ! -f /etc/debian_version ]; then
    echo "This script targets Debian/Ubuntu. /etc/debian_version not found." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "${SCRIPT_DIR}/analyzer.py" ]; then
    echo "analyzer.py not found next to this script (looked in ${SCRIPT_DIR})." >&2
    exit 1
fi

echo "==> Installing prerequisites"
apt-get update -qq
apt-get install -y --no-install-recommends python3 ca-certificates >/dev/null

echo "==> Creating service user '${SVC_USER}'"
if ! id "${SVC_USER}" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SVC_USER}"
fi

echo "==> Installing files to ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}/cache"
install -m 0644 "${SCRIPT_DIR}/analyzer.py" "${INSTALL_DIR}/analyzer.py"
if [ -f "${SCRIPT_DIR}/refresh_masters.py" ]; then
    install -m 0644 "${SCRIPT_DIR}/refresh_masters.py" "${INSTALL_DIR}/refresh_masters.py"
fi
# Seed cache from any existing cache/ directory next to this script
if [ -d "${SCRIPT_DIR}/cache" ]; then
    n_files=$(find "${SCRIPT_DIR}/cache" -type f -name 'masters_*.json' 2>/dev/null | wc -l)
    if [ "$n_files" -gt 0 ]; then
        echo "    seeding $n_files masters_*.json files into ${INSTALL_DIR}/cache/"
        cp -rn "${SCRIPT_DIR}/cache/." "${INSTALL_DIR}/cache/" 2>/dev/null || true
    fi
fi
chown -R "${SVC_USER}:${SVC_USER}" "${INSTALL_DIR}"

echo "==> Writing environment file ${ENV_FILE} (mode 0600)"
mkdir -p "$(dirname "${ENV_FILE}")"
cat > "${ENV_FILE}" <<EOF
ADDICTED_API_KEY=${API_KEY}
ADDICTED_HOST=${HOST}
ADDICTED_PORT=${PORT}
EOF
chmod 0600 "${ENV_FILE}"
chown root:root "${ENV_FILE}"

echo "==> Writing systemd unit /etc/systemd/system/${SVC_NAME}.service"
cat > "/etc/systemd/system/${SVC_NAME}.service" <<EOF
[Unit]
Description=Addicted - League of Legends Game Analyzer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/analyzer.py --api-key \${ADDICTED_API_KEY} --host \${ADDICTED_HOST} --port \${ADDICTED_PORT}
Restart=on-failure
RestartSec=5
# Allow binding privileged ports (<1024) without running as root
AmbientCapabilities=CAP_NET_BIND_SERVICE
# Light sandboxing only — anything stricter has caused weird IO/DNS issues
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/cache

[Install]
WantedBy=multi-user.target
EOF

echo "==> Reloading systemd, enabling and starting service"
systemctl daemon-reload
systemctl enable "${SVC_NAME}" >/dev/null
systemctl restart "${SVC_NAME}"
sleep 1

echo
echo "----------------------------------------------------------------"
systemctl --no-pager --full status "${SVC_NAME}" || true
echo "----------------------------------------------------------------"
echo
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Service:  ${SVC_NAME}"
echo "URL:      http://${IP_ADDR:-<host-ip>}:${PORT}"
echo "Logs:     journalctl -u ${SVC_NAME} -f"
echo "Restart:  sudo systemctl restart ${SVC_NAME}"
echo "Stop:     sudo systemctl stop ${SVC_NAME}"
echo "Update:   re-run this script after changing analyzer.py"
echo
echo "Note: if there's a firewall, allow inbound TCP ${PORT} (e.g. sudo ufw allow ${PORT}/tcp)"
