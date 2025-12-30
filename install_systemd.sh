#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="sfs-jam-monitor.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing ${SERVICE_NAME} from: ${SRC_DIR}"
sudo cp "${SRC_DIR}/${SERVICE_NAME}" "/etc/systemd/system/${SERVICE_NAME}"

echo "Reloading systemd daemon..."
sudo systemctl daemon-reload

echo "Enabling service to start at boot..."
sudo systemctl enable "${SERVICE_NAME}"

echo "Starting service..."
sudo systemctl restart "${SERVICE_NAME}"

echo
echo "Done."
echo "Check status:"
echo "  systemctl status ${SERVICE_NAME} --no-pager"
echo
echo "View logs:"
echo "  journalctl -u ${SERVICE_NAME} -f"
