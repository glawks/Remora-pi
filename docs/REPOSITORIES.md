# Remora repository split

- [Remora Pi](https://github.com/glawks/Remora-pi) — Python aiohttp HTTP/WebSocket control server, PWA fallback, Wayland/uinput adapter, QR pairing, launcher, and installation scripts.
- [Remora Android](https://github.com/glawks/Remora) — native Rust/Slint client for Android.

The Python service remains installed as `~/.local/share/pi-remote` and managed by the existing user service `pi-remote.service`. The new repo name does **not** require renaming the installed service or changing its local data paths.

## Compatibility contract

- Default endpoint `http://steamlink.local:3100` and WebSocket `/ws`.
- Paired devices live in a separate file: `~/.local/share/pi-remote-data/devices.sqlite3`. Do not publish, move, or wipe it.
- Pairing codes are one-time; existing browser tokens continue to work.
- The Android app will pair independently on first use.
- Don't expose TCP 3100 to the public internet. Use trusted LAN or private TLS/Tailscale.
- Native Android websocket clients must supply an Origin matching the Pi server's accepted host.
- The original web interface can remain as a fallback.

Keep new server features backward-compatible where feasible. Ship protocol changes with matching native client changes and automated tests.
