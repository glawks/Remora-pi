#!/usr/bin/env bash
set -euo pipefail
if [[ $EUID -eq 0 ]]; then
  echo 'Run this installer as the Raspberry Pi desktop user, not root.' >&2
  exit 1
fi
SRC="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/pi-remote"
UNIT="$HOME/.config/systemd/user/pi-remote.service"
if [[ "${XDG_SESSION_TYPE:-}" != "wayland" ]]; then
  echo 'Note: built for Wayland/labwc; make sure the Pi desktop uses Wayland.'
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo 'This installer requires Raspberry Pi OS/Debian with apt.' >&2
  exit 1
fi
if ! /usr/bin/python3 -c 'import aiohttp, evdev, tkinter, qrcode, PIL' >/dev/null 2>&1; then
  echo 'Installing Debian Python packages including QR desktop support (requires sudo)...'
  sudo apt-get update
  sudo apt-get install -y python3-aiohttp python3-evdev python3-tk python3-qrcode python3-pil
else
  echo 'Python packages already installed; skipping apt.'
fi
if ! command -v wl-copy >/dev/null 2>&1 || ! command -v wl-paste >/dev/null 2>&1; then
  echo 'Installing Wayland clipboard tools (wl-clipboard)...'
  sudo apt-get install -y wl-clipboard
fi
if ! id -nG | tr ' ' '\n' | grep -qx 'remote-mouse-uinput'; then
  echo 'WARNING: User does not appear in remote-mouse-uinput group.'
  echo 'Check /dev/uinput permissions. If needed, sign out/in after joining the group.'
fi
install -d -m 755 "$DEST/static" "$HOME/.config/systemd/user"
install -m 755 "$SRC/server.py" "$DEST/server.py"
install -m 755 "$SRC/qr_display.py" "$DEST/qr_display.py"
# Install the entire allowlisted web asset set, including Android home-screen icons.
install -m 644 "$SRC/static/"* "$DEST/static/"
cat > "$UNIT" <<'UNIT'
[Unit]
Description=Pi Remote (persistent phone pairing for Wayland)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/.local/share/pi-remote/server.py serve --host 0.0.0.0 --port 3100
WorkingDirectory=%h/.local/share/pi-remote
Restart=always
RestartSec=3
NoNewPrivileges=true
# Optional trusted HTTPS proxy origin and TLS key/certificate paths.
EnvironmentFile=-%h/.config/pi-remote/runtime.env

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable pi-remote.service
# A running 0.1.0 process must restart to load the new icon routes.
systemctl --user restart pi-remote.service
# Normal user desktop autostart, not a permanent screen overlay. When there is
# already a paired phone, qr_display.py exits without opening a window.
# Raspberry Pi OS labwc also starts XDG autostart .desktop entries.
install -d -m 755 "$HOME/.config/autostart" "$HOME/.local/share/applications"
cat > "$HOME/.config/autostart/pi-remote-qr.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=Pi Remote QR Pairing (first-time only)
Comment=Show pairing QR on the Pi desktop only when no phones are trusted
Exec=/usr/bin/python3 $DEST/qr_display.py
Icon=$DEST/static/icon-192.png
Terminal=false
StartupNotify=false
X-GNOME-Autostart-enabled=true
ENTRY
cat > "$HOME/.local/share/applications/pi-remote-add-phone.desktop" <<ENTRY
[Desktop Entry]
Type=Application
Name=Pi Remote - Add a phone
Comment=Display a temporary QR code to pair an additional phone
Exec=/usr/bin/python3 $DEST/qr_display.py --force
Icon=$DEST/static/icon-192.png
Terminal=false
StartupNotify=false
Categories=Utility;
ENTRY
# On upgrade, do not force open the QR window: it is intentionally hidden if
# the user has already paired a device. New phones can be added via the menu.
sleep 1
systemctl --user --no-pager --full status pi-remote.service || true
printf '\nOpen on phone: http://%s:3100\n' "$(hostname -I | awk '{print $1}')"
echo "Manual pairing code: python3 '$DEST/server.py' pair"
echo 'QR popup: open "Pi Remote - Add a phone" on your Pi desktop.'
echo 'For first-time installs, the QR popup opens automatically on the next desktop login.' 
echo 'After testing, stop the old program: systemctl --user disable --now remote-mouse.service'
