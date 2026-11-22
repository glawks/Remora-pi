# Changelog

## 0.3.0
- Home dashboard with temperature, live CPU use, memory, uptime, and favourites.
- Installed Linux applications discovered from desktop entries; authenticated server-generated app IDs and PNG icons.
- Favourite pinning, ordering, hiding/unhiding and searching, stored per phone.
- Moonlight already-running detection, automatically open remote controls after launch and return to touchpad after exit.
- Optional systemd power controls with explicit server-side opt-in, one-use short-lived nonce and phone confirmation.
- Preserves permanent pairing, QR workflow, port 3100, `/dev/uinput` Wayland input and user systemd service.

## 0.2.1
- Applications tab with a large one-tap Moonlight launcher and detected Chromium, Firefox, VLC and Files shortcuts.
- Authenticated, allowlisted application launches through the Pi desktop, with per-app cooldown and launch feedback.
- Detect Moonlight Qt using installed desktop entries or the `moonlight-qt` binary; preserve Wayland session environment.
- Refresh applications from phone; network-first app JS/CSS updates for HTTPS service-worker installations.
- Existing pairing database, QR handling, input backend and port 3100 remain unchanged.

## 0.2.0
- Advanced multi-touch gestures and stored per-phone sensitivity/preferences.
- WebSocket authentication timeout, stable reconnect, heartbeat and disconnect input cleanup.
- Keyboard shortcuts, Wayland clipboard commands, Moonlight navigation and media keys.
- Improved Android UI, manifest, secure-context service worker, and optional TLS/proxy support.
- Preserves old device pairing database and localStorage token key.

## 0.1.2
- Temporary QR pairing window that closes after a new phone connects.

## 0.1.1
- Browser/favicon and Android home-screen icons.

## 0.1.0
- LAN-only touchpad, keyboard, restricted `/dev/uinput`, persistent phone pairing.
