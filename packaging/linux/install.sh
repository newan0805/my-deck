#!/usr/bin/env bash
# Install the packaged LAN Deck for the current user (no sudo).
#   ./install.sh              install, add to the app menu, start at login, and start it now
#   ./install.sh --uninstall  remove it again (your settings in ~/.config/lan-deck are kept)
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
data="${XDG_DATA_HOME:-$HOME/.local/share}"
config="${XDG_CONFIG_HOME:-$HOME/.config}"
bin="$HOME/.local/bin/lan-deck"
launcher="$data/applications/lan-deck.desktop"
icon="$data/icons/hicolor/256x256/apps/lan-deck.png"
autostart="$config/autostart/lan-deck.desktop"

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user, not with sudo." >&2
  exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
  pkill -f "$bin" 2>/dev/null || true
  rm -f "$bin" "$launcher" "$icon" "$autostart"
  echo "LAN Deck removed. Settings are kept in $config/lan-deck."
  exit 0
fi

pkill -f "$bin" 2>/dev/null || true
install -Dm755 "$here/lan-deck" "$bin"
install -Dm644 "$here/lan-deck.png" "$icon"
mkdir -p "$(dirname "$launcher")" "$(dirname "$autostart")"
cat > "$launcher" <<EOF
[Desktop Entry]
Type=Application
Name=LAN Deck
Comment=Control this computer from your phone
Exec=$bin
Icon=lan-deck
Terminal=false
Categories=Network;RemoteAccess;
EOF
cat > "$autostart" <<EOF
[Desktop Entry]
Type=Application
Name=LAN Deck
Exec=$bin --background
X-GNOME-Autostart-enabled=true
NoDisplay=true
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$data/applications" >/dev/null 2>&1 || true

echo "LAN Deck installed. It starts automatically when you log in."
command -v playerctl >/dev/null || echo "Tip: install playerctl for media controls (for example: sudo apt install playerctl)."
if [ "${XDG_CURRENT_DESKTOP:-}" != "${XDG_CURRENT_DESKTOP/GNOME/}" ] && [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
  echo "GNOME on Wayland: run ./install-gnome-extension.sh once, then log out and in, so the deck can list and switch windows."
fi

nohup "$bin" >/dev/null 2>&1 &
echo "Starting LAN Deck: your browser opens the control page with the QR code for your phone."
