# Remora Pi — Pi Remote v0.3.0

**Remora Pi** is the Raspberry Pi companion server for the [Remora native Android client](https://github.com/glawks/Remora). It retains the proven Pi Remote v0.3.0 Python server, Wayland/uinput, pairing, browser fallback and Moonlight/app control. The native client connects to this existing protocol; it is developed independently.

A Raspberry Pi 5 remote control for Wayland/labwc. A phone/browser controls the Pi using `/dev/uinput` virtual mouse and keyboard devices. This is an independent implementation, **not copied from** `@velor/remote-mouse`.

## Update from v0.2.1 (or earlier paired installation)

**Your existing phone stays paired** on the normal `http://steamlink.local:3100` address: the updater preserves the separate database at `~/.local/share/pi-remote-data/devices.sqlite3` and the phone's existing `pi_remote_device_token_v1` browser-storage key. There is no automatic credential expiry.

1. Upload the Remora Pi source ZIP to the Pi using WinSCP.
2. As your normal Pi user (not root), run:

   ```bash
   unzip -o Remora-pi-v0.3.0.zip
   cd Remora-pi
   bash install.sh
   ```

3. Open **the exact same URL as before** on your phone and reload the page. Port 3100 and HTTP stay unchanged by this update. **Do not clear Chrome site data**.
4. Open **Home** to see Pi status and launch Moonlight, or **Apps** to browse desktop programs and pin favourites. The original touchpad, QR and keyboard functions remain available. No QR scan is necessary for your existing phone.

The installer reuses the previously configured `remote-mouse-uinput` group permissions, installs `wl-clipboard` for clipboard operations, copies files and restarts only `pi-remote.service`. It does not enable the original Remote Mouse or modify your original `remote-mouse.service`.

### New in v0.3.0: Dashboard and customisable apps

- **Home** shows Pi CPU temperature, CPU load (after the second sample), memory use, uptime, quick-launch favourite apps, and optional power buttons. All readings come from the Pi's own kernel files; some temperature sensors may be unavailable.
- **Apps** discovers installed applications using local `.desktop` entries. Search, hide/unhide, pin/unpin favourites and reorder pinned apps using the ↑/↓ buttons. These preferences are saved **on this phone** under the same site origin; they do not touch your paired-device token or Pi's database.
- Recognised apps' **PNG icons** are displayed when available locally; otherwise the app displays a safe fallback icon. App icons are fetched by an authenticated request and never expose pairing tokens in image URLs.
- The phone sends **server-generated app IDs** only; it cannot choose an executable, path or command. The server resolves IDs to installed desktop files and uses `gio launch` / `gtk-launch` without a shell. Moonlight still supports a `moonlight-qt` binary fallback.
- **Moonlight** is highlighted on Home. Launching it switches to the Moonlight navigation tab; when Pi Remote detects it has run and then exited, the remote switches back to Touchpad. If Moonlight is already running, another instance is not started; Pi Remote opens navigation instead. Detection is best-effort (based on the process names available on the Pi).
- **Restart / Shutdown** on Home are disabled by default. To opt in, run the following as your *normal Pi desktop user*:

  ```bash
  mkdir -p ~/.config/pi-remote
  touch ~/.config/pi-remote/runtime.env
  chmod 600 ~/.config/pi-remote/runtime.env
  # If the setting is not already present, add this line:
  echo 'PI_REMOTE_ALLOW_POWER=1' >> ~/.config/pi-remote/runtime.env
  systemctl --user restart pi-remote.service
  ```

  Don't add duplicate `PI_REMOTE_ALLOW_POWER` lines; if one exists, edit its value instead. Existing entries such as `PI_REMOTE_PUBLIC_ORIGIN` should remain unchanged. On the phone, a power action requires pressing the button and accepting a separate confirmation; the server also issues a short-lived, one-use confirmation nonce. Linux `systemd` may refuse shutdown/reboot depending on your user's permissions, and a shutdown may require manually switching the Pi back on. To disable power controls, change the setting to `0` and restart the service.

- **Unchanged:** phone pairing, short-lived QR pairing, HTTPS/PWA support, Bluetooth/Wi-Fi-independent LAN operation, app port 3100, Wayland `/dev/uinput` mouse, keyboard, clipboard, and the existing reconnect logic.

The Pi must be logged into its graphical session for launching apps. Apps open **on the Pi**, not on your phone. If launching fails, check `journalctl --user -u pi-remote.service -n 50 --no-pager` and confirm Moonlight is installed (`command -v moonlight-qt`). After updating Pi Remote, reload the phone page; on an installed HTTPS PWA, close and reopen it after the worker update.

### What's new

- **Advanced touchpad gestures**: configurable speed (0.5–3.5×), single-finger tap/cursor, two-finger vertical scrolling, two-finger tap for right-click, double-tap-and-hold for drag, a dedicated scroll strip, drag toggle, optional haptics and reverse scrolling.
- **Connection reliability**: exponential backoff, WebSocket heartbeat, connection/auth timeout, auto-reconnect after Pi restart, Wi-Fi interruption and phone sleep, explicit Reconnect button, and no replay of old mouse actions. The server automatically releases drag when the connection closes.
- **Keyboard**: a text field (US/ASCII keyboard emulation), Enter/Tab/Esc/Backspace/Delete/arrows, Ctrl+A/C/V/Z, Alt+Tab, key shortcuts.
- **Clipboard**: copy or paste up to 4,000 Unicode characters (maximum 12 KB UTF-8) into the *Pi's Wayland desktop clipboard* with `wl-copy`, read the Pi clipboard with `wl-paste`, and try to read/write the phone clipboard when the browser permits. **Manual long-press Paste/Copy in the phone text field works without HTTPS.** The app never reads the Pi clipboard without a button press.
- **Moonlight navigation**: a directional pad (hold arrows to repeat), OK/Enter, Esc, Tab, F11, Super+D, volume and play/pause. These are **keyboard/media keys sent to the focused application on the Pi**, not Moonlight's internal remote-control protocol; applicability depends on the target application.
- **Android home-screen experience**: standalone layout, updated manifest, Android icon, install prompt (when supported), and a service worker that caches only the app UI on trustworthy HTTPS origins. Offline UI is not remote-control connectivity: the Pi still needs to be reachable.
- Existing **QR pairing popup** remains: only opens on first setup or when explicitly adding a phone, and closes after a newly paired device connects.

## Install from scratch

Run `bash install.sh` as the Pi desktop account (not root). Use `http://steamlink.local:3100` or `http://PI-LAN-IP:3100` from your phone on the same trusted LAN. On first desktop login a QR popup appears if no phones are trusted; scan and tap Pair. For manual setup run:

```bash
python3 ~/.local/share/pi-remote/server.py pair
```

To pair another phone, use the Pi desktop's **Pi Remote - Add a phone** menu item or run in the graphical Pi session:

```bash
python3 ~/.local/share/pi-remote/qr_display.py --force
```

The eight-digit QR/manual code lasts ten minutes and is one-use. The phone credential remains valid until explicitly revoked.

## Proper Android PWA with trusted HTTPS (optional)

**Important limitation:** Chrome on Android does not treat `http://steamlink.local:3100` as a secure origin. On that address you can add a home-screen **shortcut**, but browser clipboard permission, service worker registration and full PWA installation are restricted. Chromium requires HTTPS or a localhost development origin for installability. Simply enabling a manifest does *not* solve this.

v0.2.1 includes an HTTPS-capable server (`server.py serve --tls-cert FILE --tls-key FILE`) and a safe option for a **trusted loopback TLS reverse proxy**. The easiest optional path, if you're willing to install Tailscale on both devices, is Tailscale Serve. It gives you a browser-trusted HTTPS address accessible **only inside your Tailscale network**, not the open internet.

1. Install and sign in to Tailscale on the Pi **and your Android phone**. Enable MagicDNS and HTTPS certificates for your tailnet. Confirm Tailscale is running.
2. On the Pi run `tailscale serve --bg 3100` and inspect the resulting HTTPS URL with `tailscale serve status`. Do **not** use `tailscale funnel` (that would make it public).
3. Configure the Pi Remote server to allow this exact HTTPS origin **only from the Pi's loopback proxy**:

   ```bash
   mkdir -p ~/.config/pi-remote
   printf 'PI_REMOTE_PUBLIC_ORIGIN=https://YOUR-PI.YOUR-TAILNET.ts.net\n' > ~/.config/pi-remote/runtime.env
   chmod 600 ~/.config/pi-remote/runtime.env
   systemctl --user restart pi-remote.service
   ```

4. On your phone, connect to Tailscale, open that **HTTPS** URL in Chrome and pair it **once** using a newly generated QR/manual code. For an HTTPS QR code, run the Pi's desktop QR utility with `--force --url https://YOUR-PI.YOUR-TAILNET.ts.net`, or use the manual `server.py pair` code while your phone is on the HTTPS site. A different scheme/host is a different browser storage origin, so the old HTTP home-screen shortcut retains its existing pairing but **does not automatically transfer its token** to HTTPS. The Pi's device database is not erased; both devices can coexist until you explicitly revoke the old entry. Once paired to the new address, reconnection is permanent there too.
5. Open Chrome's menu → **Install app** or **Add to Home screen**. Supported Android Chrome versions may show an install prompt in Settings. The standalone app, cacheable UI and phone clipboard API can now operate on the secure origin.

Tailscale Serve's exact URL depends on your own tailnet. It does not require publicly exposing port 3100. For other trusted HTTPS deployments, pass a certificate/key pair to the server and keep its origin stable. Never trust arbitrary certificates, don't forward the Pi Remote port on your router, and don't use a public reverse proxy for an input-control service.

### Practical clipboard notes

- `Paste text to Pi` sends the contents of the app's text field to `wl-copy`, then issues Ctrl+V into the **currently focused Pi window**. This supports Unicode if the receiving app supports clipboard paste. Focus the desired field first.
- `Copy text to Pi clipboard` updates the Pi clipboard without pressing Ctrl+V.
- `Get Pi clipboard` reads the current Pi clipboard into the remote's text field. To copy that text to Android, tap `Copy field to phone clipboard` on HTTPS or manually select and copy on HTTP.
- `Get phone clipboard` is only available when Chrome grants clipboard access on a trusted secure origin. On HTTP you can **long-press the text field → Paste**, then tap **Paste text to Pi**.
- If `wl-copy` fails, ensure the Pi desktop user is logged in to Wayland/labwc and has access to its runtime socket. Clipboard commands need a running desktop session; basic mouse/keyboard via `/dev/uinput` do not require `wl-copy`.

## Useful CLI commands

```bash
python3 ~/.local/share/pi-remote/server.py devices    # list trusted phones
python3 ~/.local/share/pi-remote/server.py revoke 1  # revoke a device by ID
python3 ~/.local/share/pi-remote/server.py pair      # manual one-use pairing code
systemctl --user status pi-remote.service
systemctl --user restart pi-remote.service
journalctl --user -u pi-remote.service -n 50 --no-pager
```

If an input error appears, verify `id -nG` lists `remote-mouse-uinput` and that you can open `/dev/uinput`; don't run the server as root. The installer does not change your restricted group/udev setup.

## Security and limitations

- Designed for your **trusted private LAN** over HTTP or authenticated Tailscale Serve over HTTPS. Do not put it on the public internet. LAN HTTP is not encrypted; don't pair or use it on untrusted Wi-Fi.
- Paired tokens are random, stored hashed in SQLite, and rechecked on every WebSocket event. Browser tokens live in site storage, not expiring cookies; a different URL or clearing site storage requires a fresh pairing.
- Mouse and keyboard input requires a Raspberry Pi OS Wayland desktop and the existing `/dev/uinput` permissions. The Pi has to be awake, online and running its desktop service; browser offline UI does not wake it.
- Some keyboard characters use US keycodes, so use **Paste text to Pi** for non-ASCII text or a non-US keyboard layout. Receiver applications must support clipboard paste.
- PWA cache contains **static UI files only**, never pairing codes, tokens, API responses or clipboard data. Clipboard and remote commands always require a live authenticated connection.
- Avoid sharing screenshots of pairing QR codes while active; they grant a trusted-device token to whomever uses them first.

## Tests

```bash
python3 -m unittest discover -s tests -v
node tests/test_frontend.cjs
```

The Python tests use a dummy input backend; the Node tests simulate a browser's pointer events and socket. Actual `/dev/uinput` and clipboard behaviour must be verified on your Raspberry Pi 5.

## Development repositories

- Raspberry Pi server: [glawks/Remora-pi](https://github.com/glawks/Remora-pi)
- Native Android client: [glawks/Remora](https://github.com/glawks/Remora)
- App protocol reference: [PI_PROTOCOL.md](https://github.com/glawks/Remora/blob/dev/docs/PI_PROTOCOL.md)

### Developer checks

```bash
python3 -m unittest discover -s tests -p test_remote.py
node --test tests/test_frontend.cjs
```

GitHub Actions runs these checks on pushes and pull requests; development bundles are attached to the workflow run.
