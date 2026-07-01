#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/cloud-mcp-server}"
SERVICE_USER="${SERVICE_USER:-$USER}"
PORT="${PORT:-8000}"

echo "Installing Cloud MCP in: $APP_DIR"
cd "$APP_DIR"

python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r mcp_server/requirements.txt

if [ ! -f mcp_server/.env ]; then
  cp .env.example mcp_server/.env
  echo "Created mcp_server/.env. Edit it before starting the service."
fi

sudo tee /etc/systemd/system/cloud-mcp.service >/dev/null <<SERVICE
[Unit]
Description=Cloud MCP Server
After=network.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/uvicorn mcp_server.main:app --host 127.0.0.1 --port $PORT --proxy-headers --forwarded-allow-ips="*"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable cloud-mcp

echo "Done. Edit $APP_DIR/mcp_server/.env, then run: sudo systemctl restart cloud-mcp"
