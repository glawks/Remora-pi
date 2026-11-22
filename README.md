# Remora Pi

**Raspberry Pi companion server for [Remora Android](https://github.com/glawks/Remora)** — a local remote-control server for Raspberry Pi OS with Wayland/labwc.

This repository is the home for the **Pi-side** service. The **Android/Rust/Slint client** is maintained separately at [glawks/Remora](https://github.com/glawks/Remora).

## Initial project setup

The repository has been initialised. The original **Pi Remote v0.3.0** source (provided separately) is ready to import; **the application files have not been pushed into this repository yet**.

When importing, preserve the original working server and web client: `server.py`, `qr_display.py`, `install.sh`, `static/`, `tests/`, `README.md` and `CHANGELOG.md`.

## Current Pi installation

- Raspberry Pi 5, Wayland/labwc, `/dev/uinput` mouse/keyboard
- Python `aiohttp` server and authenticated WebSocket
- Default URL: `http://steamlink.local:3100`
- Pairing QR/manual code, remembered device credentials, reconnect support
- Browser touchpad, keyboard, clipboard, Moonlight controls, application launcher

**Do not delete or overwrite** your existing per-device database in `~/.local/share/pi-remote-data/devices.sqlite3`. It is outside the deployed application directory and is intentionally not versioned.

## Branches

- `main`: stable Pi-side source and releases
- `dev`: improvements and compatibility testing before merging to stable

## Security

The service sends mouse and keyboard input to the logged-in Pi desktop. Run it only on your trusted LAN or over a controlled private network; do not port-forward it to the public internet. Do not commit device pairing tokens, runtime environment files, SQLite databases, or TLS private keys.

## Android integration

See the existing [Remora Android protocol reference](https://github.com/glawks/Remora/blob/dev/docs/PI_PROTOCOL.md) for the current server endpoints and authentication contract. Native Android support should reuse the server protocol rather than rewrite or replace the deployed Pi service.

## Source status

The original source is being staged for import from the supplied v0.3.0 ZIP. This README is an initial placeholder until the complete source is pushed.
