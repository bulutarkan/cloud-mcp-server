#!/usr/bin/env bash
set -euo pipefail

WITH_VNC_PACKAGES=0
if [[ "${1:-}" == "--with-vnc-packages" ]]; then
  WITH_VNC_PACKAGES=1
elif [[ $# -gt 0 ]]; then
  echo "Usage: $0 [--with-vnc-packages]" >&2
  exit 2
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This helper currently supports Debian/Ubuntu apt-based systems." >&2
  exit 1
fi

sudo apt-get update
sudo apt-get install -y xdotool wmctrl xclip imagemagick dbus-x11

if [[ $WITH_VNC_PACKAGES -eq 1 ]]; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y xfce4 tigervnc-standalone-server tigervnc-common
fi

if [[ ! -x /snap/bin/chromium ]] && ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  if ! command -v snap >/dev/null 2>&1; then
    sudo apt-get install -y snapd
  fi
  sudo snap install chromium
fi

# snap-confine requires this root-owned private directory. Normal snapd boot
# setup creates it, but aggressive /tmp cleanup scripts sometimes remove it.
if [[ -x /snap/bin/chromium ]]; then
  sudo install -d -m 0700 -o root -g root /tmp/snap-private-tmp
fi

cat <<'EOF'

Cloud MCP desktop dependencies are installed.

Next steps:
  1. Make sure a real X11 desktop is running (for example TigerVNC + XFCE on :1).
  2. Set these in mcp_server/.env if your values differ:
       CLOUD_MCP_DESKTOP_DISPLAY=:1
       CLOUD_MCP_DESKTOP_XAUTHORITY=/home/YOUR_USER/.Xauthority
  3. Optional persistent Chromium profile override:
       CLOUD_MCP_CHROMIUM_PROFILE=/home/YOUR_USER/snap/chromium/common/cloud-mcp-profile
  4. Restart Cloud MCP, then call desktop_capabilities and desktop_observe.

Security:
  - Do not expose VNC directly to the public Internet. Prefer localhost/SSH or a private VPN such as Tailscale.
  - The persistent Chromium profile contains authenticated sessions and must be protected like a credential.
  - If desktop_capabilities reports Chromium password store mode "basic", saved browser passwords are not encrypted by a desktop keyring. Configure a secret service/keyring before using Chromium's saved-password feature.
EOF

if [[ $WITH_VNC_PACKAGES -eq 1 ]]; then
  cat <<'EOF'

XFCE/TigerVNC packages were installed, but no VNC server was automatically exposed or configured.
Create your VNC password and start a private display yourself, for example:
  vncpasswd
  vncserver :1 -geometry 1680x1050 -depth 24 -localhost yes
EOF
fi
