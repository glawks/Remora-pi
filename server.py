#!/usr/bin/env python3
"""Pi Remote: a clean-room, LAN-only Wayland/uinput remote with persistent pairing."""
from __future__ import annotations

import argparse
import asyncio
import configparser
from collections import defaultdict, deque
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sqlite3
import sys
import time
from urllib.parse import urlsplit

from aiohttp import web, WSMsgType

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PI_REMOTE_DATA", Path.home() / ".local" / "share" / "pi-remote-data")).expanduser()
DB_PATH = DATA_DIR / "devices.sqlite3"
MAX_EVENTS_PER_SECOND = 180
MAX_CLIPBOARD_CHARS = 4000
MAX_APPS = 140
POWER_ACTIONS = {"reboot": "reboot", "shutdown": "poweroff"}
# Only user-initiated, explicit key combinations can hold modifiers.
ALLOWED_MODIFIERS = {"ctrl", "alt", "shift", "super"}
NAV_KEYS = {"enter", "backspace", "tab", "escape", "space", "up", "down", "left", "right", "home", "end", "delete", "pageup", "pagedown", "f11", "playpause", "volumeup", "volumedown", "mute", "a", "c", "d", "v", "x", "z"}



def db_connect():
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("CREATE TABLE IF NOT EXISTS devices (id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, created INTEGER NOT NULL, last_seen INTEGER NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS pairing (code_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL)")
    # Preserve existing v0.1.1 phones. This field records successful WebSocket
    # authentication so the temporary QR popup closes only after a connection.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "connected_at" not in columns:
        conn.execute("ALTER TABLE devices ADD COLUMN connected_at INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    try:
        DB_PATH.chmod(0o600)
    except OSError:
        pass
    return conn


def issue_pairing_code(conn, lifetime_seconds=600):
    """Create a new, one-use QR/manual code; invalidate previously displayed codes."""
    code = f"{secrets.randbelow(100_000_000):08d}"
    expires = int(time.time()) + lifetime_seconds
    conn.execute("DELETE FROM pairing")
    conn.execute("INSERT INTO pairing (code_hash, expires) VALUES (?, ?)", (digest(code), expires))
    conn.commit()
    return code, expires


def paired_count(conn):
    return conn.execute("SELECT count(*) FROM devices").fetchone()[0]


def has_new_connected_device(conn, after_id):
    """True only after a newly paired device authenticated on the control socket."""
    return bool(conn.execute(
        "SELECT 1 FROM devices WHERE id > ? AND connected_at > 0 LIMIT 1", (after_id,)
    ).fetchone())


def newest_device_id(conn):
    return conn.execute("SELECT COALESCE(MAX(id),0) FROM devices").fetchone()[0]


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def find_device(conn, token):
    if not isinstance(token, str) or len(token) > 150 or len(token) < 30:
        return None
    token_hash = digest(token)
    row = conn.execute("SELECT id, name, token_hash FROM devices WHERE token_hash=?", (token_hash,)).fetchone()
    if row and hmac.compare_digest(row[2], token_hash):
        return (row[0], row[1])
    return None


def local_hostname(host_header):
    # Do not accept a public internet hostname rebinding to a private IP.
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host.split("]", 1)[0][1:]
    else:
        host = host.split(":", 1)[0]
    if host in ("localhost", socket.gethostname().lower()) or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def check_same_origin(request):
    origin = request.headers.get("Origin")
    host = request.headers.get("Host", "")
    # Optional Tailscale Serve / other trusted loopback-only TLS proxy.
    # No X-Forwarded-* values are trusted: an attacker on the LAN cannot
    # impersonate the local proxy. Browser Origin must be an exact match.
    public_origin = os.environ.get("PI_REMOTE_PUBLIC_ORIGIN", "").rstrip("/").lower()
    if public_origin and request.remote:
        try:
            local_proxy = ipaddress.ip_address(request.remote).is_loopback
        except ValueError:
            local_proxy = False
        if local_proxy:
            parsed = urlsplit(public_origin)
            if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
                raise web.HTTPInternalServerError(text="Invalid PI_REMOTE_PUBLIC_ORIGIN")
            proxy_host = urlsplit("//" + host).hostname
            if proxy_host == parsed.hostname or (origin and origin.lower() == public_origin):
                if not origin or origin.lower() == public_origin:
                    return
                raise web.HTTPForbidden(text="Cross-origin requests disabled")
    if not local_hostname(host):
        raise web.HTTPForbidden(text="Use a local IP address or .local hostname")
    if origin:
        try:
            o = urlsplit(origin)
            if o.scheme != request.scheme or o.netloc.lower() != request.host.lower() or not o.hostname:
                raise ValueError("origin mismatch")
        except ValueError:
            raise web.HTTPForbidden(text="Cross-origin requests disabled")


class DummyInput:
    def __init__(self):
        self.calls = []

    def move(self, dx, dy):
        self.calls.append(("move", dx, dy))

    def click(self, button):
        self.calls.append(("click", button))

    def button(self, button, down):
        self.calls.append(("button", button, down))

    def scroll(self, amount):
        self.calls.append(("scroll", amount))

    def key(self, name):
        self.calls.append(("key", name))

    def type_text(self, text):
        self.calls.append(("text", text))

    def combo(self, key, modifiers):
        self.calls.append(("combo", key, tuple(modifiers)))

    def clipboard_set(self, text):
        self.calls.append(("clipboard-set", text))
        return True, "Text copied to Pi clipboard"

    def clipboard_get(self):
        self.calls.append(("clipboard-get",))
        return True, "Example clipboard content"

    def close(self):
        pass


class LinuxInput:
    def __init__(self):
        try:
            from evdev import UInput, ecodes
        except ImportError as exc:
            raise RuntimeError("Install python3-evdev (sudo apt install python3-evdev)") from exc
        self.e = ecodes
        self.buttons = {"left": ecodes.BTN_LEFT, "right": ecodes.BTN_RIGHT, "middle": ecodes.BTN_MIDDLE}
        self.keys = {
            "enter": ecodes.KEY_ENTER, "backspace": ecodes.KEY_BACKSPACE,
            "tab": ecodes.KEY_TAB, "escape": ecodes.KEY_ESC, "space": ecodes.KEY_SPACE,
            "up": ecodes.KEY_UP, "down": ecodes.KEY_DOWN,
            "left": ecodes.KEY_LEFT, "right": ecodes.KEY_RIGHT,
            "home": ecodes.KEY_HOME, "end": ecodes.KEY_END,
            "delete": ecodes.KEY_DELETE, "pageup": ecodes.KEY_PAGEUP,
            "pagedown": ecodes.KEY_PAGEDOWN, "ctrl": ecodes.KEY_LEFTCTRL,
            "alt": ecodes.KEY_LEFTALT, "shift": ecodes.KEY_LEFTSHIFT,
            "super": ecodes.KEY_LEFTMETA, "f11": ecodes.KEY_F11,
            "playpause": ecodes.KEY_PLAYPAUSE, "volumeup": ecodes.KEY_VOLUMEUP,
            "volumedown": ecodes.KEY_VOLUMEDOWN, "mute": ecodes.KEY_MUTE,
        }
        for c in "abcdefghijklmnopqrstuvwxyz":
            self.keys[c] = getattr(ecodes, "KEY_" + c.upper())
        for digit in "0123456789":
            self.keys[digit] = getattr(ecodes, "KEY_" + digit)
        self.keys.update({"-": ecodes.KEY_MINUS, "=": ecodes.KEY_EQUAL,
                          "[": ecodes.KEY_LEFTBRACE, "]": ecodes.KEY_RIGHTBRACE,
                          ";": ecodes.KEY_SEMICOLON, "'": ecodes.KEY_APOSTROPHE,
                          ",": ecodes.KEY_COMMA, ".": ecodes.KEY_DOT,
                          "/": ecodes.KEY_SLASH, "\\": ecodes.KEY_BACKSLASH,
                          "`": ecodes.KEY_GRAVE})
        caps_mouse = {
            ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL],
            ecodes.EV_KEY: list(self.buttons.values())
        }
        try:
            self.mouse = UInput(caps_mouse, name="Pi Remote Virtual Mouse")
            self.keyboard = UInput({ecodes.EV_KEY: sorted(set(self.keys.values()))}, name="Pi Remote Virtual Keyboard")
        except OSError as exc:
            raise RuntimeError("Cannot open /dev/uinput. Check remote-mouse-uinput group membership and permissions.") from exc

    def move(self, dx, dy):
        for axis, value in ((self.e.REL_X, dx), (self.e.REL_Y, dy)):
            if value:
                self.mouse.write(self.e.EV_REL, axis, value)
        self.mouse.syn()

    def button(self, button, down):
        self.mouse.write(self.e.EV_KEY, self.buttons[button], 1 if down else 0)
        self.mouse.syn()

    def click(self, button):
        self.button(button, True)
        self.button(button, False)

    def scroll(self, amount):
        self.mouse.write(self.e.EV_REL, self.e.REL_WHEEL, amount)
        self.mouse.syn()

    def key(self, name):
        code = self.keys.get(name)
        if code is None:
            return
        self.keyboard.write(self.e.EV_KEY, code, 1)
        self.keyboard.syn()
        self.keyboard.write(self.e.EV_KEY, code, 0)
        self.keyboard.syn()

    def type_text(self, text):
        shifted = {'!':'1','@':'2','#':'3','$':'4','%':'5','^':'6','&':'7','*':'8','(':'9',')':'0',
                   '_':'-','+':'=','{':'[','}':']',':':';','"':"'",'<':',','>':'.','?':'/','|':'\\','~':'`'}
        for ch in text[:200]:
            if ch == '\n':
                self.key("enter")
                continue
            if ch == '\t':
                self.key("tab")
                continue
            if ch == ' ':
                self.key("space")
                continue
            lower = ch.lower()
            required_shift = ch.isalpha() and ch.isupper()
            if ch in shifted:
                lower = shifted[ch]
                required_shift = True
            code = self.keys.get(lower)
            if code is None:
                continue  # not representable on the selected US keyboard layout
            if required_shift:
                self.keyboard.write(self.e.EV_KEY, self.keys["shift"], 1)
            self.keyboard.write(self.e.EV_KEY, code, 1)
            self.keyboard.syn()
            self.keyboard.write(self.e.EV_KEY, code, 0)
            if required_shift:
                self.keyboard.write(self.e.EV_KEY, self.keys["shift"], 0)
            self.keyboard.syn()

    def combo(self, key, modifiers):
        codes = [self.keys[m] for m in modifiers]
        keycode = self.keys[key]
        try:
            for code in codes:
                self.keyboard.write(self.e.EV_KEY, code, 1)
            self.keyboard.write(self.e.EV_KEY, keycode, 1)
            self.keyboard.syn()
            self.keyboard.write(self.e.EV_KEY, keycode, 0)
            self.keyboard.syn()
        finally:
            for code in reversed(codes):
                self.keyboard.write(self.e.EV_KEY, code, 0)
            self.keyboard.syn()

    @staticmethod
    def _clipboard_env():
        env = os.environ.copy()
        runtime = env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env["XDG_RUNTIME_DIR"] = runtime
        if not env.get("WAYLAND_DISPLAY"):
            # The systemd user service can start before the desktop imports its
            # environment. Only select an existing socket owned by this user.
            for candidate in ("wayland-0", "wayland-1"):
                path = Path(runtime) / candidate
                if path.is_socket() and path.stat().st_uid == os.getuid():
                    env["WAYLAND_DISPLAY"] = candidate
                    break
        return env

    def clipboard_set(self, text):
        if not shutil.which("wl-copy"):
            return False, "wl-copy is unavailable. Install wl-clipboard."
        try:
            result = subprocess.run(["wl-copy", "--type", "text/plain;charset=utf-8"],
                                    input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, timeout=3,
                                    env=self._clipboard_env(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False, "Cannot access the Pi desktop clipboard"
        if result.returncode != 0:
            return False, "Pi desktop clipboard is unavailable; check Wayland session"
        return True, "Text copied to Pi clipboard"

    def clipboard_get(self):
        if not shutil.which("wl-paste"):
            return False, "wl-paste is unavailable. Install wl-clipboard."
        try:
            result = subprocess.run(["wl-paste", "--no-newline", "--type", "text/plain"],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    timeout=3, env=self._clipboard_env(), check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False, "Cannot access the Pi desktop clipboard"
        if result.returncode != 0:
            return False, "No plain text available in Pi clipboard"
        return True, result.stdout[:MAX_CLIPBOARD_CHARS * 4].decode("utf-8", "replace")[:MAX_CLIPBOARD_CHARS]

    def close(self):
        self.mouse.close()
        self.keyboard.close()



# Only locally approved application families are exposed to paired devices.
# The browser never sends filenames, shell commands, or command arguments.
APP_DEFINITIONS = {
    "moonlight": {
        "name": "Moonlight", "description": "Game streaming on your Pi",
        "desktop": ("com.moonlight_stream.Moonlight.desktop", "moonlight-qt.desktop", "moonlight.desktop"),
        "binaries": ("moonlight-qt",),
    },
    "chromium": {
        "name": "Chromium", "description": "Web browser",
        "desktop": ("chromium.desktop", "chromium-browser.desktop"),
        "binaries": ("chromium", "chromium-browser"),
    },
    "firefox": {
        "name": "Firefox", "description": "Web browser",
        "desktop": ("firefox.desktop", "org.mozilla.firefox.desktop"),
        "binaries": ("firefox",),
    },
    "vlc": {
        "name": "VLC", "description": "Media player",
        "desktop": ("vlc.desktop", "org.videolan.VLC.desktop"),
        "binaries": ("vlc",),
    },
    "files": {
        "name": "Files", "description": "File manager",
        "desktop": ("pcmanfm.desktop", "pcmanfm-qt.desktop", "thunar.desktop", "org.gnome.Nautilus.desktop"),
        "binaries": ("pcmanfm", "pcmanfm-qt", "thunar", "nautilus"),
    },
}


class AppLauncher:
    """Discover installed Linux .desktop entries, launch by *server-generated ID*.

    No client-supplied paths, executable names or arguments are executed.
    """

    @staticmethod
    def desktop_dirs():
        home = Path.home()
        dirs = [Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share"))) / "applications"]
        xdg_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
        dirs += [Path(x) / "applications" for x in xdg_dirs.split(":") if x]
        dirs += [home / ".local/share/flatpak/exports/share/applications",
                 Path("/var/lib/flatpak/exports/share/applications")]
        return list(dict.fromkeys(dirs))

    @staticmethod
    def parse_desktop(path):
        try:
            if not path.is_file() or path.stat().st_size > 100000:
                return None
            cfg = configparser.ConfigParser(interpolation=None, strict=False)
            cfg.read(path, encoding="utf-8")
            e = cfg["Desktop Entry"]
            if e.get("Type", "Application") != "Application":
                return None
            if e.getboolean("Hidden", False) or e.getboolean("NoDisplay", False):
                return None
            if not e.get("Exec") and not e.getboolean("DBusActivatable", False):
                return None
            def clean(value, length):
                return "".join(ch for ch in value if ch.isprintable() and ch not in "<>\\").strip()[:length]
            name = clean(e.get("Name", path.stem), 65)
            if not name:
                return None
            return {"name": name, "description": clean(e.get("Comment", ""), 100),
                    "icon": e.get("Icon", "")[:180]}
        except (OSError, ValueError, configparser.Error, KeyError, UnicodeError):
            return None

    @classmethod
    def valid_desktop(cls, path):
        return cls.parse_desktop(path) is not None

    def find(self, app_id):
        definition = APP_DEFINITIONS.get(app_id)
        if definition:
            for directory in self.desktop_dirs():
                for filename in definition["desktop"]:
                    candidate = directory / filename
                    if candidate.is_file() and self.valid_desktop(candidate):
                        if shutil.which("gio"):
                            return ("desktop", str(candidate))
                        if shutil.which("gtk-launch"):
                            return ("gtk-launch", filename.removesuffix(".desktop"))
            for binary in definition["binaries"]:
                executable = shutil.which(binary)
                if executable:
                    return ("binary", executable)
            return None
        item = self.catalogue().get(app_id)
        return item["launcher"] if item else None

    @staticmethod
    def desktop_id(path):
        return "desktop-" + hashlib.sha256(str(path).encode()).hexdigest()[:20]

    def catalogue(self):
        """The authoritative, bounded ID->launcher map built from local files."""
        result = {}
        seen_paths = set()
        for app_id, definition in APP_DEFINITIONS.items():
            found = self._find_known(app_id)
            if found and found[0] == "desktop":
                seen_paths.add(Path(found[1]).resolve())
            result[app_id] = {
                "id": app_id, "name": definition["name"],
                "description": definition["description"], "available": found is not None,
                "launcher": found, "icon": "", "running": self.is_running(app_id) if app_id == "moonlight" else False,
            }
            if found and found[0] == "desktop":
                data = self.parse_desktop(Path(found[1]))
                if data:
                    result[app_id]["icon"] = data["icon"]
        for directory in self.desktop_dirs():
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.desktop"))[:300]:
                if len(result) >= MAX_APPS:
                    break
                try:
                    resolved = path.resolve(strict=True)
                    if resolved in seen_paths or not path.is_file():
                        continue
                    # Only desktop entries inside the discovered applications directory.
                    if not resolved.is_relative_to(directory.resolve()):
                        continue
                except (OSError, RuntimeError):
                    continue
                data = self.parse_desktop(path)
                if not data:
                    continue
                # Skip duplicates of the well-known apps even if multiple launchers are installed.
                if path.name in {n for e in APP_DEFINITIONS.values() for n in e["desktop"]}:
                    continue
                key = self.desktop_id(resolved)
                if shutil.which("gio"):
                    launcher = ("desktop", str(path))
                elif shutil.which("gtk-launch"):
                    launcher = ("gtk-launch", path.stem)
                else:
                    continue
                seen_paths.add(resolved)
                result[key] = {"id": key, **data, "available": True,
                               "launcher": launcher, "running": False}
        return result

    def _find_known(self, app_id):
        definition = APP_DEFINITIONS.get(app_id)
        if not definition:
            return None
        for directory in self.desktop_dirs():
            for filename in definition["desktop"]:
                candidate = directory / filename
                if candidate.is_file() and self.valid_desktop(candidate):
                    if shutil.which("gio"):
                        return ("desktop", str(candidate))
                    if shutil.which("gtk-launch"):
                        return ("gtk-launch", filename.removesuffix(".desktop"))
        for binary in definition["binaries"]:
            executable = shutil.which(binary)
            if executable:
                return ("binary", executable)
        return None

    @staticmethod
    def is_running(app_id):
        if app_id != "moonlight":
            return False
        proc = Path("/proc")
        try:
            for entry in proc.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != os.getuid():
                        continue
                    comm = (entry / "comm").read_text(errors="ignore").strip().lower()
                    if comm in ("moonlight", "moonlight-qt"):
                        return True
                    # Flatpak may use a launcher with a more generic comm.
                    if comm == "flatpak":
                        cmd = (entry / "cmdline").read_bytes()[:4096].replace(b"\0", b" ")
                        if b"com.moonlight_stream.Moonlight" in cmd:
                            return True
                except (OSError, UnicodeError):
                    pass
        except OSError:
            pass
        return False

    def list_apps(self):
        result = []
        for item in self.catalogue().values():
            # Always show Moonlight, even if absent, with an explanatory label.
            if not item["available"] and item["id"] != "moonlight":
                continue
            result.append({key: item[key] for key in ("id", "name", "description", "available", "running")})
            result[-1]["hasIcon"] = self.icon_path(item["icon"]) is not None
        result.sort(key=lambda item:(item["id"]!="moonlight", item["name"].casefold()))
        return result

    @staticmethod
    def icon_path(icon):
        # Serve a *PNG* only from standard local icon/theme directories.
        if not icon or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", icon):
            return None
        base = icon[:-4] if icon.endswith(".png") else icon
        roots = [Path("/usr/share/pixmaps"), Path("/usr/local/share/pixmaps")]
        themes = [Path("/usr/share/icons"), Path.home()/".local/share/icons"]
        for theme_root in themes:
            if theme_root.is_dir():
                for theme in list(theme_root.iterdir())[:24]:
                    for sz in ("128x128", "64x64", "48x48", "256x256", "scalable"):
                        roots.append(theme / sz / "apps")
        for root in roots:
            path = root / (base + ".png")
            try:
                if path.is_file() and 0 < path.stat().st_size <= 512_000:
                    return path
            except OSError:
                continue
        return None

    @staticmethod
    def desktop_env():
        env = LinuxInput._clipboard_env()
        bus = Path(env["XDG_RUNTIME_DIR"]) / "bus"
        if not env.get("DBUS_SESSION_BUS_ADDRESS") and bus.is_socket():
            env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + str(bus)
        return env

    def launch(self, app_id):
        if app_id == "moonlight" and self.is_running("moonlight"):
            return True, "Moonlight is already running", True
        found = self.find(app_id)
        if not found:
            return False, "Application not detected on the Pi", False
        env = self.desktop_env()
        if not env.get("WAYLAND_DISPLAY") and not env.get("DISPLAY"):
            return False, "Pi desktop session isn't available yet. Log in on the Pi.", False
        kind, target = found
        cmd = ["gio", "launch", target] if kind == "desktop" else (["gtk-launch", target] if kind == "gtk-launch" else [target])
        try:
            if kind == "binary":
                subprocess.Popen(cmd, env=env, start_new_session=True,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
                return True, "Launch requested", False
            completed = subprocess.run(cmd, env=env, timeout=6, check=False,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE)
            if completed.returncode:
                err = completed.stderr.decode("utf-8", "replace").strip()[:160]
                return False, "Desktop launcher could not start the app" + (": " + err if err else ""), False
            return True, "Launch requested", False
        except (OSError, subprocess.TimeoutExpired):
            return False, "Application launch failed. Check the Pi desktop session.", False


def system_snapshot():
    """Read-only local system diagnostics; never run shell commands."""
    output = {"uptime": None, "cpuTemp": None, "memoryPercent": None,
              "cpuPercent": None, "powerEnabled": os.environ.get("PI_REMOTE_ALLOW_POWER", "") == "1"}
    try:
        output["uptime"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    try:
        value = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip())
        output["cpuTemp"] = round(value / 1000, 1) if value > 1000 else float(value)
    except (OSError, ValueError):
        pass
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                values[k] = int(rest.strip().split()[0])
        if values.get("MemTotal", 0) and "MemAvailable" in values:
            output["memoryPercent"] = round(100*(1-values["MemAvailable"]/values["MemTotal"]))
    except (OSError, ValueError, IndexError):
        pass
    try:
        first_line = Path("/proc/stat").read_text().splitlines()[0].split()
        if first_line[0] == "cpu":
            samples = [int(x) for x in first_line[1:]]
            output["cpuSample"] = [sum(samples), samples[3] + (samples[4] if len(samples)>4 else 0)]
    except (OSError, ValueError, IndexError):
        pass
    return output


def perform_power_action(action):
    """Requires explicit opt-in, an authenticated socket and a one-time confirmation.

    systemd/logind may still refuse the operation if the desktop user lacks rights.
    """
    if action not in POWER_ACTIONS or os.environ.get("PI_REMOTE_ALLOW_POWER", "") != "1":
        return False, "Power controls disabled on this Pi"
    try:
        cmd = ["systemctl", "--no-ask-password", POWER_ACTIONS[action]]
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8, check=False)
        if p.returncode:
            return False, (p.stderr.decode("utf-8", "replace").strip()[:160] or "Permission denied by systemd")
        return True, "Requested " + action
    except (OSError, subprocess.TimeoutExpired):
        return False, "Power command failed or timed out"

def make_app(input_backend=None, app_launcher=None):
    app = web.Application(client_max_size=4096)
    app["db"] = db_connect()
    app["input"] = input_backend or (DummyInput() if os.environ.get("PI_REMOTE_DRY_RUN") == "1" else LinuxInput())
    app["launcher"] = app_launcher if app_launcher is not None else AppLauncher()
    app["app_last_launch"] = {}
    app["power_confirmation"] = {}
    app["failures"] = defaultdict(deque)

    async def on_cleanup(a):
        a["input"].close()
        a["db"].close()

    app.on_cleanup.append(on_cleanup)

    async def index(request):
        check_same_origin(request)
        response = web.FileResponse(APP_DIR / "static" / "index.html")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def static(request):
        check_same_origin(request)
        name = request.match_info["name"]
        if name not in {
            "app.js", "style.css", "manifest.webmanifest", "favicon.ico",
            "favicon-32.png", "apple-touch-icon.png", "icon-192.png",
            "icon-512.png", "icon-maskable-512.png", "sw.js",
        }:
            raise web.HTTPNotFound()
        response = web.FileResponse(APP_DIR / "static" / name)
        if name.endswith(".webmanifest"):
            response.content_type = "application/manifest+json"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = ("no-cache, must-revalidate" if name in
                                             {"app.js", "style.css", "manifest.webmanifest", "sw.js"}
                                             else "public, max-age=86400")
        return response

    async def service_worker(request):
        check_same_origin(request)
        response = web.FileResponse(APP_DIR / "static" / "sw.js")
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    async def favicon(request):
        check_same_origin(request)
        response = web.FileResponse(APP_DIR / "static" / "favicon.ico")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def verify(request):
        check_same_origin(request)
        body = await request.json()
        device = find_device(app["db"], body.get("token"))
        if not device:
            return web.json_response({"ok": False, "reason": "revoked_or_missing"}, status=401)
        return web.json_response({"ok": True, "device": device[1]})

    async def pair(request):
        check_same_origin(request)
        ip = request.remote or "unknown"
        now = time.monotonic()
        history = app["failures"][ip]
        while history and now - history[0] > 60:
            history.popleft()
        if len(history) >= 5:
            return web.json_response({"error": "Too many attempts. Try later."}, status=429)
        history.append(now)
        body = await request.json()
        code = str(body.get("code", "")).strip()
        if not re.fullmatch(r"\d{8}", code):
            return web.json_response({"error": "Enter the eight-digit pairing code"}, status=400)
        conn = app["db"]
        conn.execute("DELETE FROM pairing WHERE expires < ?", (int(time.time()),))
        row = conn.execute("SELECT code_hash FROM pairing WHERE code_hash=? AND expires>=?", (digest(code), int(time.time()))).fetchone()
        if not row:
            conn.commit()
            return web.json_response({"error": "Invalid or expired pairing code"}, status=403)
        label = re.sub(r"[^a-zA-Z0-9 _.-]", "", str(body.get("name", "Phone")))[:40].strip() or "Phone"
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM pairing WHERE code_hash=?", (digest(code),))
        conn.execute("INSERT INTO devices (name, token_hash, created, last_seen) VALUES (?, ?, ?, ?)",
                     (label, digest(token), int(time.time()), int(time.time())))
        conn.commit()
        return web.json_response({"ok": True, "token": token, "name": label})

    async def app_icon(request):
        check_same_origin(request)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise web.HTTPBadRequest()
        if not find_device(app["db"], body.get("token")):
            raise web.HTTPUnauthorized()
        key = body.get("id")
        if not isinstance(key, str) or len(key) > 64:
            raise web.HTTPBadRequest()
        found = await asyncio.to_thread(app["launcher"].catalogue)
        item = found.get(key)
        if item is None:
            raise web.HTTPNotFound()
        path = app["launcher"].icon_path(item["icon"])
        if path is None:
            raise web.HTTPNotFound()
        response = web.FileResponse(path)
        response.content_type = "image/png"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def ws_control(request):
        check_same_origin(request)
        # The websocket must originate from a page on this very server.
        if not request.headers.get("Origin"):
            raise web.HTTPForbidden(text="Missing origin")
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=16384, receive_timeout=65)
        await ws.prepare(request)
        authorized = False
        left_down = False
        events = deque()
        last_status = 0.0
        last_cpu_sample = None
        input_dev = app["input"]
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
                    continue
                try:
                    payload = json.loads(msg.data)
                    kind = payload.get("type")
                    if not authorized:
                        if kind != "auth":
                            await ws.close(code=1008, message=b"auth required")
                            break
                        device = find_device(app["db"], payload.get("token"))
                        if not device:
                            await ws.send_json({"type": "denied"})
                            await ws.close(code=1008, message=b"not paired")
                            break
                        authorized = True
                        app["ws_tokens"][id(ws)] = payload["token"]
                        app["db"].execute("UPDATE devices SET last_seen=?, connected_at=? WHERE id=?",
                                          (int(time.time()), int(time.time()), device[0]))
                        app["db"].commit()
                        await ws.send_json({"type": "ready"})
                        continue
                    # Revoked devices cannot retain an established connection.
                    if not find_device(app["db"], payload.get("token") if kind == "auth" else request.app["ws_tokens"].get(id(ws), "")):
                        # Tokens are deliberately not stored in log files.
                        await ws.send_json({"type": "denied"})
                        break
                    now = time.monotonic()
                    while events and events[0] < now - 1:
                        events.popleft()
                    if len(events) >= MAX_EVENTS_PER_SECOND:
                        continue
                    events.append(now)
                    if kind == "ping":
                        await ws.send_json({"type": "pong"})
                    elif kind == "system-status":
                        if now-last_status >= 1.5:
                            last_status = now
                            info = await asyncio.to_thread(system_snapshot)
                            sample = info.pop("cpuSample", None)
                            if sample and last_cpu_sample:
                                delta_total = sample[0] - last_cpu_sample[0]
                                delta_idle = sample[1] - last_cpu_sample[1]
                                if delta_total > 0:
                                    info["cpuPercent"] = max(0, min(100, round(100*(1-delta_idle/delta_total))))
                            last_cpu_sample = sample
                            info["moonlightRunning"] = await asyncio.to_thread(app["launcher"].is_running, "moonlight")
                            await ws.send_json({"type":"system-info", "data":info})
                    elif kind == "power-prepare":
                        action = payload.get("action")
                        if action not in POWER_ACTIONS or os.environ.get("PI_REMOTE_ALLOW_POWER", "") != "1":
                            await ws.send_json({"type":"power-status", "ok":False,
                                                "message":"Power controls are disabled; see Settings"})
                        else:
                            challenge = secrets.token_urlsafe(20)
                            app["power_confirmation"][id(ws)] = (action, digest(challenge), now + 20)
                            await ws.send_json({"type":"power-confirm", "action":action, "nonce":challenge})
                    elif kind == "power-execute":
                        action, nonce = payload.get("action"), payload.get("nonce")
                        record = app["power_confirmation"].pop(id(ws), None)
                        if (not record or not isinstance(nonce, str) or not isinstance(action, str)
                                or record[0] != action or record[2] < now
                                or not hmac.compare_digest(record[1], digest(nonce))):
                            await ws.send_json({"type":"power-status", "ok":False,
                                                "message":"Confirmation expired. Start again."})
                            continue
                        # Acknowledge the confirmed request before systemd stops the service.
                        await ws.send_json({"type":"power-status", "ok":True,
                                            "message":"Requesting " + action + "…"})
                        async def run_power(a, current_ws):
                            await asyncio.sleep(0.5)
                            ok, message = await asyncio.to_thread(perform_power_action, a)
                            if not ok and not current_ws.closed:
                                await current_ws.send_json({"type":"power-status", "ok":False, "message":message})
                        asyncio.create_task(run_power(action, ws))
                    elif kind == "apps-list":
                        apps = await asyncio.to_thread(app["launcher"].list_apps)
                        await ws.send_json({"type": "apps", "apps": apps})
                    elif kind == "app-launch":
                        app_id = payload.get("id")
                        if not isinstance(app_id, str) or len(app_id) > 64 or not re.fullmatch(r"[a-zA-Z0-9_-]+", app_id):
                            await ws.send_json({"type":"app-status", "ok":False,
                                                "message":"Invalid application ID"})
                            continue
                        if not await asyncio.to_thread(app["launcher"].find, app_id):
                            await ws.send_json({"type":"app-status", "ok":False,
                                                "message":"Application is not installed or approved"})
                            continue
                        last = app["app_last_launch"].get(app_id, -10000)
                        if now - last < 4:
                            await ws.send_json({"type":"app-status", "ok":False,
                                                "message":"Already requested; try again shortly"})
                            continue
                        app["app_last_launch"][app_id] = now
                        result = await asyncio.to_thread(app["launcher"].launch, app_id)
                        ok, message = result[:2]
                        already = result[2] if len(result) > 2 else False
                        await ws.send_json({"type":"app-status", "ok":ok,
                                            "id":app_id, "message":message, "alreadyRunning":already})
                    elif kind == "move":
                        dx, dy = payload.get("dx"), payload.get("dy")
                        if type(dx) in (int, float) and type(dy) in (int, float):
                            if -250 <= dx <= 250 and -250 <= dy <= 250:
                                input_dev.move(round(dx), round(dy))
                    elif kind == "scroll":
                        amount = payload.get("amount")
                        if type(amount) is int and -8 <= amount <= 8 and amount:
                            input_dev.scroll(amount)
                    elif kind == "click":
                        button = payload.get("button")
                        if button in ("left", "right", "middle"):
                            input_dev.click(button)
                    elif kind == "drag":
                        down = payload.get("down")
                        if type(down) is bool and down != left_down:
                            input_dev.button("left", down)
                            left_down = down
                    elif kind == "key":
                        name = payload.get("name")
                        if isinstance(name, str) and name in NAV_KEYS:
                            input_dev.key(name)
                    elif kind == "combo":
                        name = payload.get("name")
                        modifiers = payload.get("modifiers")
                        if (isinstance(name, str) and name in NAV_KEYS and
                                isinstance(modifiers, list) and len(modifiers) <= 4 and
                                all(isinstance(m, str) and m in ALLOWED_MODIFIERS for m in modifiers) and
                                len(set(modifiers)) == len(modifiers) and name not in modifiers):
                            input_dev.combo(name, modifiers)
                    elif kind == "clipboard-set":
                        value = payload.get("value")
                        action = payload.get("action", "copy")
                        if (isinstance(value, str) and 0 < len(value) <= MAX_CLIPBOARD_CHARS and
                                len(value.encode("utf-8")) <= 12000 and action in ("copy", "paste")):
                            ok, message = await asyncio.to_thread(input_dev.clipboard_set, value)
                            if ok and action == "paste":
                                input_dev.combo("v", ["ctrl"])
                            await ws.send_json({"type":"clipboard-status", "ok":ok, "message":
                                                ("Pasted into Pi" if ok and action == "paste" else message)})
                    elif kind == "clipboard-get":
                        ok, value = await asyncio.to_thread(input_dev.clipboard_get)
                        await ws.send_json({"type": "clipboard-data" if ok else "clipboard-status",
                                            "ok":ok, "value":value if ok else "", "message":"" if ok else value})
                    elif kind == "text":
                        text_value = payload.get("value")
                        if isinstance(text_value, str) and 0 < len(text_value) <= 200:
                            input_dev.type_text(text_value)
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    continue
        finally:
            request.app["ws_tokens"].pop(id(ws), None)
            request.app["power_confirmation"].pop(id(ws), None)
            if left_down:
                input_dev.button("left", False)
        return ws

    app["ws_tokens"] = {}
    app.router.add_get("/", index)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/static/{name}", static)
    app.router.add_post("/api/verify", verify)
    app.router.add_post("/api/pair", pair)
    app.router.add_post("/api/app-icon", app_icon)
    app.router.add_get("/ws", ws_control)
    return app


def cmd_pair(_args):
    conn = db_connect()
    code, _expires = issue_pairing_code(conn)
    conn.close()
    print("\nOne-time phone pairing code: " + code)
    print("Valid for 10 minutes, and only one use. Subsequent reconnects require no code.\n")


def cmd_devices(_args):
    conn = db_connect()
    rows = conn.execute("SELECT id,name,created,last_seen FROM devices ORDER BY id").fetchall()
    if not rows:
        print("No paired devices")
    for id_, name, created, last in rows:
        print(f"{id_}: {name} | paired {time.strftime('%Y-%m-%d', time.localtime(created))} | last seen {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}")
    conn.close()


def cmd_revoke(args):
    conn = db_connect()
    cur = conn.execute("DELETE FROM devices WHERE id=?", (args.device_id,))
    conn.commit()
    print("Revoked" if cur.rowcount else "No such device")
    conn.close()


def main():
    p = argparse.ArgumentParser(description="Pi Remote — permanent phone pairing on Raspberry Pi Wayland")
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("serve", help="Run web remote")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=3100)
    s.add_argument("--tls-cert", default=os.environ.get("PI_REMOTE_TLS_CERT"), help="Optional PEM TLS certificate (requires --tls-key)")
    s.add_argument("--tls-key", default=os.environ.get("PI_REMOTE_TLS_KEY"), help="Optional PEM TLS private key")
    sub.add_parser("pair", help="Print a one-time pairing code")
    sub.add_parser("devices", help="List trusted phones")
    r = sub.add_parser("revoke", help="Revoke a paired phone")
    r.add_argument("device_id", type=int)
    args = p.parse_args()
    if args.command == "pair":
        cmd_pair(args)
    elif args.command == "devices":
        cmd_devices(args)
    elif args.command == "revoke":
        cmd_revoke(args)
    elif args.command == "serve":
        try:
            if bool(args.tls_cert) != bool(args.tls_key):
                raise RuntimeError("Supply both --tls-cert and --tls-key to use HTTPS")
            context = None
            if args.tls_cert:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                context.load_cert_chain(args.tls_cert, args.tls_key)
            web.run_app(make_app(), host=args.host, port=args.port, ssl_context=context,
                        print=lambda s: print(s, flush=True))
        except (RuntimeError, OSError, ssl.SSLError) as exc:
            print(f"Startup error: {exc}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
