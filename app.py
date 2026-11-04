#!/usr/bin/env python3
"""LAN Deck: control your computer's apps, windows and media from a phone on the same network.

Linux support lives in this file. Windows and macOS swap in their own functions from
backends/ at startup (see _install_backend at the bottom).
"""
from __future__ import annotations

import collections
import ipaddress
import json
import queue
import socket
import sys
import mimetypes
import functools
import ast
import re
import shlex
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent
# Packaged builds (PyInstaller) unpack bundled files to sys._MEIPASS.
RESOURCES = Path(getattr(sys, "_MEIPASS", ROOT))
STATIC = RESOURCES / "deck" / "static"
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def config_dir() -> Path:
    if os.environ.get("DECK_CONFIG_DIR"):
        return Path(os.environ["DECK_CONFIG_DIR"])
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home()) / "LAN Deck"
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / "LAN Deck"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lan-deck"


CONFIG_FILE = config_dir() / "config.json"
LOG: collections.deque[str] = collections.deque(maxlen=400)
LOG_FILE: Path | None = None  # set by the desktop launcher


def log(message: str) -> None:
    """Keep recent lines for the control page and print them when there is a console."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {message}"
    LOG.append(line)
    if LOG_FILE:
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
                LOG_FILE.replace(LOG_FILE.with_suffix(".old.log"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    if sys.stdout:  # windowed builds have no console
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass


def load_config() -> dict:
    """Persistent settings shared with the control app. A PIN is created once so phones stay paired."""
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        config = {}
    if not str(config.get("token", "")).isdigit() or len(str(config.get("token"))) != 6:
        config["token"] = f"{secrets.randbelow(1_000_000):06d}"
        config.setdefault("port", 8765)
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
            if not IS_WINDOWS:
                CONFIG_FILE.chmod(0o600)
        except OSError:
            pass
    return config


def save_config(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
    if not IS_WINDOWS:
        CONFIG_FILE.chmod(0o600)


_CONFIG = load_config()
TOKEN = os.environ.get("DECK_TOKEN") or str(_CONFIG["token"])
PORT = int(os.environ.get("DECK_PORT") or _CONFIG.get("port") or 8765)
STARTED = time.time()
_APPS_CACHE: tuple[float, list[dict]] = (0.0, [])
_APPS_LOCK = threading.Lock()
_AUTH_ATTEMPTS: dict[str, tuple[int, float]] = {}
_AUTH_LOCK = threading.Lock()
_SCRATCHPAD_WINDOWS: set[str] = set()
_HIDDEN_HYPR_WINDOWS: dict[str, str] = {}
_HIDDEN_NIRI_WINDOWS: dict[str, tuple[str, str]] = {}


def run(args: list[str], timeout: float = 2.0) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def gvariant_true(value: str) -> bool:
    return value.startswith("(") and value.rstrip("), ").endswith("true")


def desktop_entries() -> list[dict]:
    global _APPS_CACHE
    now = time.monotonic()
    if now - _APPS_CACHE[0] < 60:
        return _APPS_CACHE[1]
    with _APPS_LOCK:
        now = time.monotonic()
        if now - _APPS_CACHE[0] >= 60:
            _APPS_CACHE = (now, _scan_desktop_entries())
        return _APPS_CACHE[1]


def _scan_desktop_entries() -> list[dict]:
    paths = [Path("/usr/share/applications"), Path.home() / ".local/share/applications"]
    apps: dict[str, dict] = {}
    for folder in paths:
        if not folder.exists():
            continue
        for f in folder.rglob("*.desktop"):
            try:
                raw = f.read_text(errors="replace").splitlines()
                section = False
                data: dict[str, str] = {}
                for line in raw:
                    if line.strip() == "[Desktop Entry]":
                        section = True
                    elif line.startswith("[") and section:
                        break
                    elif section and "=" in line and not line.lstrip().startswith("#"):
                        k, v = line.split("=", 1)
                        data[k] = v
                name = data.get("Name", "").strip()
                command = data.get("Exec", "").strip()
                if not name or not command or data.get("NoDisplay", "false").lower() == "true":
                    continue
                app_id = f.stem
                icon = data.get("Icon", "")
                apps.setdefault(app_id, {"id": app_id, "name": name, "icon": icon, "icon_url": f"/icon/{urllib.parse.quote(app_id, safe='')}" if resolve_icon(icon) else "", "wm_class": data.get("StartupWMClass", ""), "exec": command, "categories": data.get("Categories", "")})
            except OSError:
                continue
    return sorted(apps.values(), key=lambda a: a["name"].casefold())


_ICON_EXTS = (".svg", ".png", ".xpm", ".webp")
_ICON_ROOTS = [Path.home() / ".local/share/icons", Path.home() / ".icons", Path("/usr/share/icons"), Path("/usr/local/share/icons")]
_ICON_INDEX_LOCK = threading.Lock()


def _icon_rank(path: Path) -> int:
    """Higher is better: scalable first, then the largest bitmap size."""
    best = 0
    for part in path.parts:
        if "scalable" in part:
            return 100_000
        m = re.match(r"(\d+)(?:x\d+)?(?:@(\d)x)?$", part)
        if m:
            best = max(best, int(m.group(1)) * int(m.group(2) or 1))
    return best


@functools.lru_cache(maxsize=1)
def _icon_themes() -> list[Path]:
    """Theme folders in the order the desktop uses them: current theme, what it inherits, hicolor, then the rest."""
    current = run(["gsettings", "get", "org.gnome.desktop.interface", "icon-theme"]).strip().strip("'")
    order: list[Path] = []
    seen: set[str] = set()

    def add(theme: str, depth: int = 0) -> None:
        if not theme or theme in seen or depth > 5:
            return
        seen.add(theme)
        dirs = [r / theme for r in _ICON_ROOTS if (r / theme).is_dir()]
        order.extend(dirs)
        for d in dirs:
            try:
                for line in (d / "index.theme").read_text(errors="replace").splitlines():
                    if line.startswith("Inherits="):
                        for parent in line.split("=", 1)[1].split(","):
                            add(parent.strip(), depth + 1)
            except OSError:
                pass

    add(current)
    add("Adwaita")
    add("hicolor")
    for root in _ICON_ROOTS:
        try:
            order.extend(d for d in sorted(root.iterdir()) if d.is_dir() and d.name not in seen)
        except OSError:
            pass
    return order


@functools.lru_cache(maxsize=1)
def _icon_index() -> list[tuple[Path, dict[str, Path]]]:
    """One walk over every theme: theme -> {icon name -> best app icon file}."""
    with _ICON_INDEX_LOCK:
        index = []
        for theme in _icon_themes():
            names: dict[str, Path] = {}
            ranks: dict[str, int] = {}
            for folder, _dirs, files in os.walk(theme, followlinks=True):
                if "apps" not in Path(folder).relative_to(theme).parts and not folder.endswith("/apps"):
                    continue
                for f in files:
                    stem, ext = os.path.splitext(f)
                    if ext.lower() not in _ICON_EXTS:
                        continue
                    path = Path(folder) / f
                    rank = _icon_rank(path.relative_to(theme))
                    if rank > ranks.get(stem, -1):
                        names[stem], ranks[stem] = path, rank
            index.append((theme, names))
        return index


@functools.lru_cache(maxsize=1024)
def resolve_icon(icon: str) -> Path | None:
    """Resolve a desktop icon name the way the desktop does (current theme first, exact names only)."""
    if not icon:
        return None
    direct = Path(icon).expanduser()
    if direct.is_absolute():
        return direct if direct.is_file() else None
    name = Path(icon).name
    # "org.gnome.Terminal" has no file extension: only strip real image extensions.
    base = name[: -len(Path(name).suffix)] if Path(name).suffix.lower() in _ICON_EXTS else name
    for _theme, names in _icon_index():
        hit = names.get(base)
        if hit and hit.is_file():
            return hit
    for folder in (Path("/usr/share/pixmaps"), Path.home() / ".local/share/pixmaps"):
        for ext in ("",) + _ICON_EXTS:
            candidate = folder / f"{base}{ext}"
            if candidate.is_file() and (ext or candidate.suffix.lower() in _ICON_EXTS):
                return candidate
    return None


def _norm(value: str) -> str:
    return value.casefold().removesuffix(".desktop").replace(" ", "").replace("-", "").replace("_", "")


def match_window_app(wm_class: str, desktop_id: str = "") -> dict | None:
    """Find the launcher for a window. GNOME's own app id (from the bridge) wins; then the
    window class against the launcher's id, StartupWMClass, name, icon and program name."""
    apps = desktop_entries()
    if desktop_id:
        wanted = desktop_id.removesuffix(".desktop")
        hit = next((a for a in apps if a["id"] == wanted), None)
        if hit:
            return hit
    key = _norm(wm_class)
    if not key:
        return None
    for field in (lambda a: a["id"], lambda a: a.get("wm_class", ""), lambda a: a["name"],
                  lambda a: a.get("icon", ""), lambda a: Path((shlex.split(a.get("exec", "")) or [""])[0]).name if a.get("exec") else "",
                  lambda a: a["id"].rsplit(".", 1)[-1]):  # org.gnome.Terminal -> terminal
        for a in apps:
            try:
                if _norm(field(a)) == key:
                    return a
            except ValueError:
                continue
    return None


def _bridge_string(response: str) -> str:
    """gdbus prints a one-string reply as ('...',); return that string."""
    value = ast.literal_eval(response)
    return value[0] if isinstance(value, tuple) else str(value)


def gnome_wayland_windows() -> tuple[list[dict], bool]:
    """Use the Deck Shell extension; retain Eval as an optional fallback."""
    if not (os.environ.get("WAYLAND_DISPLAY") and shutil.which("gdbus")):
        return [], False
    response = run(["gdbus", "call", "--session", "--dest", "io.chainit.LANDeck", "--object-path", "/io/chainit/LANDeck", "--method", "io.chainit.LANDeck.ListWindows"], timeout=3.0)
    if response.startswith("("):
        try:
            items = json.loads(_bridge_string(response))
            result = []
            for w in items:
                title, wm_class = str(w.get("title", "")), str(w.get("wm_class", ""))
                folded = f"{title} {wm_class}".casefold()
                if not title or any(helper in folded for helper in ("mutter guard", "mutter-x11-frames", "ibus-x11", "ibus-xim", "clipboard", "xdg-desktop-portal", "com.rastersoft.ding", "desktop icons")):
                    continue
                matched = match_window_app(wm_class, str(w.get("app", "")))
                result.append({"id": f"g:{int(w['id'])}", "title": title, "class": wm_class, "app_id": matched["id"] if matched else "", "app_name": matched["name"] if matched else "", "icon_url": matched["icon_url"] if matched else "", "minimized": bool(w.get("minimized"))})
            return result, True
        except (ValueError, SyntaxError, json.JSONDecodeError, TypeError):
            pass
    script = "JSON.stringify(global.get_window_actors().map(a => { const w=a.meta_window; return {id:w.get_id(), title:w.get_title()||'', wm_class:w.get_wm_class()||'', minimized:w.minimized}; }).filter(w => w.title))"
    response = run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell", "--method", "org.gnome.Shell.Eval", script], timeout=3.0)
    if not response.startswith("(true,"):
        return [], False
    try:
        payload = ast.literal_eval(response.split(",", 1)[1].rsplit(")", 1)[0].strip())
        items = json.loads(payload)
    except (ValueError, SyntaxError, json.JSONDecodeError):
        return [], False
    result = []
    for w in items:
        title, wm_class = str(w.get("title", "")), str(w.get("wm_class", ""))
        folded = f"{title} {wm_class}".casefold()
        if any(helper in folded for helper in ("mutter guard", "mutter-x11-frames", "ibus-x11", "ibus-xim", "clipboard", "xdg-desktop-portal", "com.rastersoft.ding", "desktop icons")):
            continue
        matched = match_window_app(wm_class)
        result.append({"id": f"g:{int(w['id'])}", "title": title, "class": wm_class, "app_id": matched["id"] if matched else "", "app_name": matched["name"] if matched else "", "icon_url": matched["icon_url"] if matched else "", "minimized": bool(w.get("minimized"))})
    return result, True


def window_record(wid: str, title: str, wm_class: str = "", minimized: bool = False, extra: dict | None = None) -> dict:
    matched = match_window_app(wm_class)
    record = {"id": wid, "title": title, "class": wm_class, "app_id": matched["id"] if matched else "", "app_name": matched["name"] if matched else "", "icon_url": matched["icon_url"] if matched else "", "minimized": minimized}
    if extra:
        record.update(extra)
    return record


def walk_sway(node: dict, result: list[dict]):
    if node.get("type") in {"con", "floating_con"} and node.get("name"):
        props = node.get("window_properties") or {}
        wm_class = props.get("class") or node.get("app_id") or ""
        result.append(window_record("s:" + str(node.get("id")), node["name"], wm_class, node.get("scratchpad_state") not in (None, "", "none")))
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        walk_sway(child, result)


def find_sway_node(node: dict, target: int) -> dict | None:
    if node.get("id") == target:
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = find_sway_node(child, target)
        if found:
            return found
    return None


def sway_windows() -> list[dict] | None:
    if not os.environ.get("SWAYSOCK") or not shutil.which("swaymsg"):
        return None
    try:
        tree = json.loads(run(["swaymsg", "-t", "get_tree", "-r"], timeout=3.0))
    except json.JSONDecodeError:
        return None
    result: list[dict] = []
    walk_sway(tree, result)
    return result


def hyprland_windows() -> list[dict] | None:
    if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") or not shutil.which("hyprctl"):
        return None
    try:
        clients = json.loads(run(["hyprctl", "-j", "clients"], timeout=3.0))
    except json.JSONDecodeError:
        return None
    return [window_record("h:" + str(w.get("address", "")), w.get("title") or w.get("initialTitle") or "", w.get("class") or w.get("initialClass") or "", bool(w.get("hidden")), {"workspace": (w.get("workspace") or {}).get("name", "")}) for w in clients if w.get("mapped", True) and (w.get("title") or w.get("initialTitle"))]


def niri_windows() -> list[dict] | None:
    if not os.environ.get("NIRI_SOCKET") or not shutil.which("niri"):
        return None
    try:
        data = json.loads(run(["niri", "msg", "--json", "windows"], timeout=3.0))
        if isinstance(data, dict):
            data = data.get("Ok", {}).get("Windows", data.get("windows", []))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    return [window_record("n:" + str(w.get("id", "")), w.get("title") or "", w.get("app_id") or "", False, {"workspace": str(w.get("workspace_id", ""))}) for w in data if w.get("title")]


def launch_desktop_app(app_id: str) -> tuple[bool, str]:
    app = next((a for a in desktop_entries() if a["id"] == app_id), None)
    if not app:
        return False, "App not found"
    try:
        argv = [x for x in shlex.split(app["exec"]) if not (x.startswith("%") or x == "%%")]
        if not argv:
            return False, "No launch command"
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True, ""
    except (OSError, ValueError) as exc:
        return False, str(exc)


_LAUNCHER_SUFFIXES = ("-stable", "-browser", "-launcher", "-wrapper", ".sh")
_PROCESS_SUFFIXES = ("-server", "-bin", ".bin", "-wrapped", "-editor")
_BACKGROUND_FLAGS = ("--gapplication-service", "--background", "--daemon", "--headless", "--type=")


def _name_variants(name: str, suffixes: tuple[str, ...]) -> set[str]:
    out, name = set(), name.casefold().strip()
    while name and name not in out:
        out.add(name)
        name = next((name[: -len(x)] for x in suffixes if name.endswith(x)), name)
    return {n for n in out if len(n) > 2}


def _exec_names(command: str) -> set[str]:
    try:
        argv = [x for x in shlex.split(command) if not x.startswith("%")]
    except ValueError:
        return set()
    while argv and (argv[0] in {"env", "/usr/bin/env"} or "=" in argv[0]):
        argv = argv[1:]
    if not argv:
        return set()
    name = Path(argv[0]).name
    if name in {"flatpak", "snap"}:
        name = argv[-1]  # app id is the last argument
    return set() if name in {"sh", "bash", "python3", "python", "java", "electron"} else {name}


def process_windows() -> list[dict]:
    """Installed apps whose program is running for this user (used when no window API is available)."""
    uid, running = os.getuid(), set()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            if proc.stat().st_uid != uid:
                continue
            args = [a.decode(errors="replace") for a in (proc / "cmdline").read_bytes().split(b"\0") if a]
            if not args or any(a.startswith(_BACKGROUND_FLAGS) for a in args[1:]):
                continue
            running |= _name_variants(Path(args[0]).name, _PROCESS_SUFFIXES)
            try:
                running |= _name_variants(Path(os.readlink(proc / "exe")).name, _PROCESS_SUFFIXES)
            except OSError:
                pass
        except OSError:
            continue
    if shutil.which("flatpak"):
        running |= {f.casefold() for f in run(["flatpak", "ps", "--columns=application"]).split()}
    result, seen = [], set()
    for app in desktop_entries():
        if any(flag in app.get("exec", "") for flag in ("--app-id=", "--app=", "--profile-directory=")):
            continue  # browser web-app shortcuts share the browser's process
        names: set[str] = set()
        for n in _exec_names(app.get("exec", "")) | {app.get("wm_class", "")}:
            names |= _name_variants(n, _LAUNCHER_SUFFIXES)
        key = frozenset(names & running)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({"id": f"p:{app['id']}", "title": app["name"], "class": "", "app_id": app["id"], "app_name": app["name"], "icon_url": app["icon_url"], "minimized": False, "process_only": True})
    return result


LABELS_FILE = CONFIG_FILE.parent / "labels.json"
_LABELS_LOCK = threading.Lock()


def _read_labels() -> dict:
    try:
        data = json.loads(LABELS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_labels(labels: dict) -> None:
    LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LABELS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(labels, indent=2) + "\n")
    tmp.replace(LABELS_FILE)  # atomic: a crash never leaves a half-written file


def labelled_windows() -> tuple[list[dict], bool, str]:
    """windows() plus the names people gave them. A name only sticks to the same window of the
    same app; names of windows that have closed are dropped."""
    items, supported, reason = windows()
    with _LABELS_LOCK:
        labels = _read_labels()
        for w in items:
            entry = labels.get(w["id"])
            if entry and entry.get("app_id", "") == w["app_id"]:
                w["label"] = entry.get("name", "")
        live = {w["id"] for w in items}
        if supported and any(not i.startswith("p:") for i in live):
            stale = [k for k in labels if not k.startswith("p:") and k not in live]
            if stale:
                for k in stale:
                    labels.pop(k, None)
                _write_labels(labels)
    return items, supported, reason


def rename_window(wid: str, name: str) -> tuple[bool, str]:
    name = " ".join(name.split())[:40]
    current = next((w for w in windows()[0] if w["id"] == wid), None)
    if not current:
        return False, "That window is no longer open"
    with _LABELS_LOCK:
        labels = _read_labels()
        if name:
            labels[wid] = {"name": name, "app_id": current["app_id"]}
        else:
            labels.pop(wid, None)
        _write_labels(labels)
    return True, ""


_LAST_BRIDGE: tuple[float, list[dict]] | None = None


def windows() -> tuple[list[dict], bool, str]:
    for provider in (sway_windows, hyprland_windows, niri_windows):
        items = provider()
        if items is not None:
            return items, True, ""
    wayland_note = ""
    if os.environ.get("WAYLAND_DISPLAY"):
        global _LAST_BRIDGE
        result, supported = gnome_wayland_windows()
        if supported:
            _LAST_BRIDGE = (time.monotonic(), result)
            return result, True, ""
        # A slow or failed bridge call must not flip the deck to guessed apps for a moment:
        # keep the last real window list for a while instead.
        if _LAST_BRIDGE and time.monotonic() - _LAST_BRIDGE[0] < 30:
            return [dict(w) for w in _LAST_BRIDGE[1]], True, ""
        if "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").casefold():
            wayland_note = "Only XWayland windows are visible until the GNOME bridge is installed. Run ./install-gnome-extension.sh once."
        else:
            wayland_note = "Only XWayland windows are visible; native Wayland windows need a compositor integration."
    if not shutil.which("xdotool") or not os.environ.get("DISPLAY"):
        fallback = process_windows()
        if fallback:
            return fallback, True, wayland_note
        reason = wayland_note or "No desktop display is available to the LAN Deck process. Start it from your logged-in graphical session."
        return [], False, reason
    # Include hidden/minimized windows so they remain available for restore/focus.
    ids = run(["xdotool", "search", "--name", "."]).splitlines()
    result = []
    for wid in ids:
        title = run(["xdotool", "getwindowname", wid])
        if not title:
            continue
        props = run(["xprop", "-id", wid, "WM_CLASS", "_NET_WM_STATE", "_NET_WM_WINDOW_TYPE"])
        quoted = __import__("re").findall(r'"([^"]+)"', props.split("WM_CLASS", 1)[-1].split("_NET_WM_STATE", 1)[0])
        wm_class = quoted[-1] if quoted else run(["xdotool", "getwindowclassname", wid])
        folded = f"{title} {wm_class}".casefold()
        if any(helper in folded for helper in ("mutter guard", "mutter-x11-frames", "ibus-x11", "ibus-xim", "clipboard", "xdg-desktop-portal", "com.rastersoft.ding", "desktop icons")):
            continue
        if any(kind in props for kind in ("_NET_WM_WINDOW_TYPE_DOCK", "_NET_WM_WINDOW_TYPE_DESKTOP", "_NET_WM_WINDOW_TYPE_NOTIFICATION", "_NET_WM_WINDOW_TYPE_TOOLTIP")):
            continue
        minimized = "_NET_WM_STATE_HIDDEN" in props
        matched = match_window_app(wm_class)
        result.append({"id": wid, "title": title, "class": wm_class, "app_id": matched["id"] if matched else "", "app_name": matched["name"] if matched else "", "icon_url": matched["icon_url"] if matched else "", "minimized": minimized})
    if os.environ.get("WAYLAND_DISPLAY"):
        # XWayland only sees some windows; add running native apps it missed.
        known = {w["app_id"] for w in result if w["app_id"]}
        result += [w for w in process_windows() if w["app_id"] not in known]
        return result, bool(result), wayland_note
    return result, True, ""


def workspace_state() -> dict:
    """Return compositor workspaces grouped by physical output when available."""
    if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
        try:
            outputs = json.loads(run(["swaymsg", "-r", "-t", "get_outputs"]))
            workspaces = json.loads(run(["swaymsg", "-r", "-t", "get_workspaces"]))
            screens = [{"name": o["name"], "workspaces": [{"id": str(w.get("num") if w.get("num") is not None else w["name"]), "name": w["name"], "active": bool(w.get("focused"))} for w in workspaces if w.get("output") == o["name"]]} for o in outputs if o.get("active")]
            return {"supported": True, "backend": "sway", "per_monitor": True, "screens": screens}
        except (ValueError, KeyError, TypeError):
            pass
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        try:
            monitors = json.loads(run(["hyprctl", "-j", "monitors"]))
            workspaces = json.loads(run(["hyprctl", "-j", "workspaces"]))
            screens = [{"name": m["name"], "workspaces": [{"id": str(w["id"]), "name": str(w["name"]), "active": bool(w.get("focused"))} for w in workspaces if w.get("monitor") == m["name"]]} for m in monitors]
            return {"supported": True, "backend": "hyprland", "per_monitor": True, "screens": screens}
        except (ValueError, KeyError, TypeError):
            pass
    if os.environ.get("NIRI_SOCKET") and shutil.which("niri"):
        try:
            raw = json.loads(run(["niri", "msg", "--json", "workspaces"]))
            workspaces = raw.get("Ok", {}).get("Workspaces", raw) if isinstance(raw, dict) else raw
            outs = json.loads(run(["niri", "msg", "--json", "outputs"]))
            if isinstance(outs, dict):
                outs = outs.get("Ok", {}).get("Outputs", outs.get("outputs", {}))
            output_names = list(outs.keys()) if isinstance(outs, dict) else [str(o.get("name")) for o in outs]
            screens = [{"name": name, "workspaces": [{"id": w["name"] if w.get("name") else str(w.get("idx", 1)), "name": w.get("name") or f"Workspace {w.get('idx', '')}", "active": bool(w.get("is_focused"))} for w in workspaces if w.get("output") == name]} for name in output_names]
            return {"supported": True, "backend": "niri", "per_monitor": True, "screens": screens}
        except (ValueError, KeyError, TypeError):
            pass
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_CURRENT_DESKTOP", "").casefold().find("gnome") >= 0 and shutil.which("gdbus"):
        response = run(["gdbus", "call", "--session", "--dest", "io.chainit.LANDeck", "--object-path", "/io/chainit/LANDeck", "--method", "io.chainit.LANDeck.ListWorkspaces"], timeout=3.0)
        if response.startswith("("):
            try:
                value = json.loads(_bridge_string(response))
                ws = [{"id": str(i), "name": f"Desktop {i + 1}", "active": i == value.get("active")} for i in range(value.get("count", 0))]
                return {"supported": True, "backend": "gnome", "per_monitor": False, "screens": [{"name": "All screens", "workspaces": ws}], "message": "GNOME shares desktops across all screens."}
            except (ValueError, SyntaxError, json.JSONDecodeError):
                pass
        script = "JSON.stringify({count:global.workspace_manager.n_workspaces,active:global.workspace_manager.get_active_workspace_index(),monitors:Array.from({length:global.display.get_n_monitors()},(_,i)=>global.display.get_monitor_plug_name(i)||('Monitor '+(i+1)))})"
        response = run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell", "--method", "org.gnome.Shell.Eval", script], timeout=3.0)
        if response.startswith("(true,"):
            try:
                value = json.loads(ast.literal_eval(response.split(",", 1)[1].rsplit(")", 1)[0].strip()))
                names = value.get("monitors") or ["All screens"]
                ws = [{"id": str(i), "name": f"Desktop {i + 1}", "active": i == value.get("active")} for i in range(value.get("count", 0))]
                return {"supported": True, "backend": "gnome", "per_monitor": False, "screens": [{"name": name, "workspaces": ws} for name in names], "message": "GNOME shares workspaces across monitors."}
            except (ValueError, SyntaxError, json.JSONDecodeError):
                pass
    if not os.environ.get("WAYLAND_DISPLAY") and shutil.which("wmctrl"):
        raw = run(["wmctrl", "-d"])
        workspaces = []
        for line in raw.splitlines():
            match = re.match(r"(\d+)\s+(\*)?\s*(\S+)", line)
            if match:
                workspaces.append({"id": match.group(1), "name": match.group(3), "active": bool(match.group(2))})
        if workspaces:
            monitors = run(["xrandr", "--listmonitors"]).splitlines()[1:] if shutil.which("xrandr") else []
            names = [line.split()[-1] for line in monitors] or ["All screens"]
            return {"supported": True, "backend": "x11", "per_monitor": False, "screens": [{"name": name, "workspaces": workspaces} for name in names], "message": "This window manager shares workspaces across monitors."}
    return {"supported": False, "backend": "", "per_monitor": False, "screens": [], "message": "This desktop does not expose workspace controls to LAN Deck."}


def workspace_action(action: str, monitor: str, workspace: str) -> tuple[bool, str]:
    if not re.fullmatch(r"[A-Za-z0-9_.: -]{1,64}", monitor) or not re.fullmatch(r"[A-Za-z0-9_.: -]{1,64}", workspace):
        return False, "Invalid screen or workspace"
    if os.environ.get("SWAYSOCK") and shutil.which("swaymsg"):
        if action == "create":
            run(["swaymsg", f'workspace "{workspace}" output "{monitor}"'])
        response = run(["swaymsg", f'workspace "{workspace}"'])
        return ("success" in response.casefold(), "Could not switch Sway workspace")
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("hyprctl"):
        if run(["hyprctl", "dispatch", "focusmonitor", monitor]).casefold().startswith("ok") is False:
            return False, "Could not focus monitor"
        command = "focusworkspaceoncurrentmonitor" if action == "switch" else "workspace"
        target = workspace if re.fullmatch(r"-?\d+", workspace) or workspace.startswith("name:") else "name:" + workspace
        response = run(["hyprctl", "dispatch", command, target])
        ok = response.strip().casefold().startswith("ok")
        return ok, "Could not switch Hyprland workspace"
    if os.environ.get("NIRI_SOCKET") and shutil.which("niri"):
        if action == "create":
            run(["niri", "msg", "action", "focus-monitor", monitor])
            state = workspace_state()
            group = next((x for x in state["screens"] if x["name"] == monitor), {})
            index = len(group.get("workspaces", []))
            run(["niri", "msg", "action", "focus-workspace", str(index + 1)])
            response = run(["niri", "msg", "action", "set-workspace-name", workspace])
            return ("handled" in response.casefold(), "Could not create Niri workspace")
        run(["niri", "msg", "action", "focus-monitor", monitor])
        response = run(["niri", "msg", "action", "focus-workspace", workspace])
        return ("handled" in response.casefold(), "Could not switch Niri workspace")
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("XDG_CURRENT_DESKTOP", "").casefold().find("gnome") >= 0:
        if action == "switch" and not workspace.isdigit():
            return False, "Invalid GNOME workspace"
        method = "SwitchWorkspace" if action == "switch" else "CreateWorkspace"
        arg = str(int(workspace)) if action == "switch" else "0"
        response = run(["gdbus", "call", "--session", "--dest", "io.chainit.LANDeck", "--object-path", "/io/chainit/LANDeck", "--method", "io.chainit.LANDeck." + method, arg], timeout=3.0)
        if response.startswith("("):
            ok = gvariant_true(response)
            return ok, "GNOME workspace control failed"
        js = f"global.workspace_manager.get_workspace_by_index({int(workspace)}).activate(global.get_current_time())" if action == "switch" else "global.workspace_manager.append_new_workspace(false,global.get_current_time()).activate(global.get_current_time())"
        response = run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell", "--method", "org.gnome.Shell.Eval", js], timeout=3.0)
        return response.startswith("(true,"), "GNOME workspace control is unavailable"
    if not os.environ.get("WAYLAND_DISPLAY") and shutil.which("wmctrl"):
        if action == "create":
            try:
                current = len(run(["wmctrl", "-d"]).splitlines())
                run(["wmctrl", "-n", str(current + 1)])
                workspace = str(current)
            except ValueError:
                return False, "Could not create X11 workspace"
        if workspace.isdigit():
            response = run(["wmctrl", "-s", workspace])
            return not bool(response), "Could not switch X11 workspace"
    return False, "Workspace controls are unavailable in this desktop"


_MEDIA_SEP = "\x1f"
_MEDIA_FIELDS = ["status", "position", "mpris:length", "title", "artist", "album", "mpris:artUrl", "volume"]


def _volume(value: str) -> float | None:
    try:
        return max(0.0, min(1.0, float(value)))
    except ValueError:
        return None


def _media_player(player: str) -> dict:
    fmt = _MEDIA_SEP.join("{{%s}}" % f for f in _MEDIA_FIELDS)
    raw = run(["playerctl", "-p", player, "metadata", "--format", fmt])
    parts = raw.split(_MEDIA_SEP) if raw else []
    if len(parts) != len(_MEDIA_FIELDS):
        parts = [run(["playerctl", "-p", player, "status"]), "", "", "", "", "", "", ""]
    status, position, length, title, artist, album, art, volume = parts

    def secs(v: str) -> float:
        try:
            return max(0.0, int(v) / 1_000_000)
        except ValueError:
            return 0.0

    art_url = ""
    if art.startswith(("http://", "https://")):
        art_url = art
    elif art.startswith("file://"):
        art_url = "/api/media/art?player=" + urllib.parse.quote(player)
    return {
        "id": player, "name": player.split(".")[0], "status": status or "Stopped",
        "playing": status == "Playing", "title": title, "artist": artist, "album": album,
        "position": secs(position), "length": secs(length), "art_url": art_url,
        "volume": _volume(volume),
    }


def media_state() -> dict:
    if not shutil.which("playerctl"):
        return {"available": False, "playing": False, "title": "", "artist": "", "player": "", "players": [], "active": ""}
    players = [_media_player(p) for p in run(["playerctl", "-l"]).splitlines() if p.strip()]
    if not players:
        return {"available": True, "playing": False, "title": "", "artist": "", "player": "", "players": [], "active": ""}
    chosen = next((p for p in players if p["playing"]), next((p for p in players if p["status"] == "Paused"), players[0]))
    return {"available": True, "playing": chosen["playing"], "title": chosen["title"], "artist": chosen["artist"],
            "player": chosen["id"], "players": players, "active": chosen["id"], "time": time.time()}


def media_art(player: str) -> Path | None:
    known = run(["playerctl", "-l"]).splitlines()
    if player not in known:
        return None
    url = run(["playerctl", "-p", player, "metadata", "mpris:artUrl"])
    if not url.startswith("file://"):
        return None
    file = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path))
    if not file.is_file() or file.stat().st_size > 8_000_000 or not image_mime(file):
        return None
    return file


def image_mime(file: Path) -> str:
    try:
        head = file.read_bytes()[:16] if file.stat().st_size < 8_000_000 else b""
    except OSError:
        return ""
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF8"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return ""


class LiveHub:
    """Pushes media/window/workspace state to connected clients over Server-Sent Events.

    Media changes are event-driven via `playerctl --follow`, with a short poll to catch seeks.
    Windows and workspaces are polled on the server and only sent when they change.
    All loops idle while nobody is connected.
    """

    INTERVALS = {"media": 1.5, "windows": 2.0, "workspaces": 3.0}

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.clients: set[queue.Queue] = set()
        self.last: dict[str, dict] = {}
        self.wake = {kind: threading.Event() for kind in self.INTERVALS}
        self.active = threading.Event()
        self.started = False

    # --- state sources ---
    @staticmethod
    def snapshot(kind: str) -> dict:
        if kind == "media":
            return media_state()
        if kind == "windows":
            items, supported, reason = labelled_windows()
            return {"windows": items, "supported": supported, "reason": reason}
        return workspace_state()

    @staticmethod
    def _media_changed(old: dict | None, new: dict) -> bool:
        if old is None:
            return True
        strip = lambda m: [{k: v for k, v in p.items() if k != "position"} for p in m.get("players", [])]
        if strip(old) != strip(new) or old.get("active") != new.get("active") or old.get("available") != new.get("available"):
            return True
        # Same tracks: only resend when a position jumped (seek), not when it advanced normally.
        elapsed = new.get("time", 0) - old.get("time", 0)
        for a, b in zip(old.get("players", []), new.get("players", [])):
            expected = a["position"] + (elapsed if a["playing"] else 0)
            if abs(b["position"] - expected) > 1.5:
                return True
        return False

    def publish(self, kind: str, data: dict) -> None:
        with self.lock:
            old = self.last.get(kind)
            changed = self._media_changed(old, data) if kind == "media" else old != data
            if not changed:
                return
            self.last[kind] = data
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                pass

    def refresh(self, *kinds: str) -> None:
        for kind in kinds or self.INTERVALS:
            self.wake[kind].set()

    # --- clients ---
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=64)
        with self.lock:
            self.clients.add(q)
            self.active.set()
            if not self.started:
                self.started = True
                for kind in self.INTERVALS:
                    threading.Thread(target=self._loop, args=(kind,), daemon=True).start()
                if shutil.which("playerctl"):
                    threading.Thread(target=self._follow_media, daemon=True).start()
            cached = dict(self.last)
        for kind in self.INTERVALS:
            if kind in cached:
                q.put((kind, cached[kind]))   # instant first paint from cache
            else:
                self.wake[kind].set()         # the loop publishes it to everyone
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)
            if not self.clients:
                self.active.clear()

    # --- workers ---
    def _loop(self, kind: str) -> None:
        while True:
            self.active.wait()
            try:
                self.publish(kind, self.snapshot(kind))
            except Exception as exc:  # keep the stream alive whatever a backend does
                log(f"live {kind} update failed: {exc}")
            self.wake[kind].wait(self.INTERVALS[kind])
            self.wake[kind].clear()

    def _follow_media(self) -> None:
        fmt = "{{playerName}}|{{status}}|{{title}}|{{artist}}|{{mpris:length}}|{{volume}}"
        while True:
            self.active.wait()
            try:
                proc = subprocess.Popen(["playerctl", "--all-players", "--follow", "metadata", "--format", fmt],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                for _ in proc.stdout:
                    self.wake["media"].set()
                    if not self.active.is_set():
                        break
                proc.kill()
            except OSError:
                pass
            time.sleep(3)


HUB = LiveHub()


class Handler(BaseHTTPRequestHandler):
    server_version = "LANDeck/0.1"

    def log_message(self, fmt, *args):
        if self.path.startswith(("/api/status", "/api/events")):
            return  # polled by the control app / long-lived stream: keep the log readable
        log(f"{self.client_address[0]}  {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not supplied:
            supplied = (urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token") or [""])[0]
        ip = self.client_address[0]
        now = time.monotonic()
        with _AUTH_LOCK:
            count, locked_until = _AUTH_ATTEMPTS.get(ip, (0, 0.0))
            if locked_until > now:
                return False
            valid = secrets.compare_digest(supplied, TOKEN)
            if valid:
                _AUTH_ATTEMPTS.pop(ip, None)
                return True
            count += 1
            _AUTH_ATTEMPTS[ip] = (0, now + 60) if count >= 8 else (count, 0.0)
            return False

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/icon/"):
            app_id = urllib.parse.unquote(path[len("/icon/"):])
            if BACKEND:
                icon = BACKEND.icon_path(app_id)
            else:
                app = next((a for a in desktop_entries() if a["id"] == app_id), None)
                icon = resolve_icon(app["icon"]) if app else None
            if not icon:
                return self.send_error(404)
            body = icon.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", image_mime(icon) or mimetypes.guess_type(icon.name)[0] or "application/octet-stream")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/health":
            return self.send_json({"ok": True, "name": "LAN Deck", "version": VERSION})
        if path == "/admin" or path.startswith("/admin/"):
            return self.admin_get(path)
        if path == "/api/media/art":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            supplied = (query.get("token") or [""])[0]
            if not secrets.compare_digest(supplied, TOKEN):
                return self.send_error(401)
            art = media_art((query.get("player") or [""])[0])
            if not art:
                return self.send_error(404)
            body = art.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", image_mime(art) or "image/png")
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/"):
            if not self.authorized():
                return self.send_json({"error": "Pair this device with your desktop"}, 401)
            if path == "/api/events":
                return self.stream_events()
            if path == "/api/status":
                with HUB.lock:
                    clients = len(HUB.clients)
                return self.send_json({"ok": True, "clients": clients, "uptime": round(time.time() - STARTED), "port": PORT,
                                       "bridge": False if BACKEND else gnome_wayland_windows()[1],
                                       "playerctl": bool(shutil.which("playerctl")), "pid": os.getpid(), "platform": sys.platform})
            if path == "/api/session":
                return self.send_json({"ok": True})
            if path == "/api/apps":
                return self.send_json({"apps": desktop_entries()})
            if path == "/api/windows":
                items, supported, reason = labelled_windows()
                return self.send_json({"windows": items, "supported": supported, "reason": reason})
            if path == "/api/workspaces":
                return self.send_json(workspace_state())
            if path == "/api/media":
                return self.send_json(media_state())
            return self.send_json({"error": "Not found"}, 404)
        rel = "index.html" if path == "/" else path.lstrip("/")
        file = (STATIC / rel).resolve()
        if not file.is_relative_to(STATIC.resolve()) or not file.is_file():
            return self.send_error(404)
        mime = "text/html; charset=utf-8" if file.suffix == ".html" else "application/javascript; charset=utf-8" if file.suffix == ".js" else "text/css; charset=utf-8" if file.suffix == ".css" else "application/manifest+json" if file.suffix == ".webmanifest" else "image/svg+xml" if file.suffix == ".svg" else "image/png" if file.suffix == ".png" else "application/octet-stream"
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = HUB.subscribe()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                try:
                    kind, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            HUB.unsubscribe(q)

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path.startswith("/admin/"):
            return self.admin_post()
        if not self.authorized():
            return self.send_json({"error": "Unauthorized"}, 401)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(min(size, 16384)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "Invalid request"}, 400)
        path = urllib.parse.urlparse(self.path).path
        threading.Timer(0.35, HUB.refresh).start()
        if path == "/api/launch":
            app_id = str(payload.get("id", ""))
            ok, error = launch_desktop_app(app_id)
            return self.send_json({"ok": ok, **({} if ok else {"error": error})}, 200 if ok else 400)
        if path == "/api/window/rename":
            ok, error = rename_window(str(payload.get("id", "")), str(payload.get("name", "")))
            if ok:
                HUB.refresh("windows")
            return self.send_json({"ok": ok, **({} if ok else {"error": error})}, 200 if ok else 404)
        if path == "/api/window/activate" and BACKEND:
            ok, error = BACKEND.activate_window(str(payload.get("id", "")))
            return self.send_json({"ok": ok, **({} if ok else {"error": error})}, 200 if ok else 400)
        if path == "/api/window/activate":
            wid = str(payload.get("id", ""))
            if wid.startswith("p:"):
                ok, error = launch_desktop_app(wid[2:])
                return self.send_json({"ok": ok, "launched": True, **({} if ok else {"error": error})}, 200 if ok else 400)
            if wid.startswith("p:"):
                ok, error = launch_desktop_app(wid[2:])
                return self.send_json({"ok": ok, "launched": True, **({} if ok else {"error": error})}, 200 if ok else 400)
            if wid.startswith("s:") and wid[2:].isdigit() and shutil.which("swaymsg"):
                con_id = str(int(wid[2:]))
                tree_raw = run(["swaymsg", "-t", "get_tree", "-r"])
                try:
                    target = find_sway_node(json.loads(tree_raw), int(con_id))
                except (ValueError, TypeError):
                    target = None
                scratchpad = target and target.get("scratchpad_state") not in (None, "", "none")
                if con_id in _SCRATCHPAD_WINDOWS or scratchpad:
                    response = run(["swaymsg", f"[con_id={con_id}] scratchpad show"])
                    _SCRATCHPAD_WINDOWS.discard(con_id)
                elif target and target.get("focused"):
                    response = run(["swaymsg", f"[con_id={con_id}] move scratchpad"])
                    _SCRATCHPAD_WINDOWS.add(con_id)
                else:
                    response = run(["swaymsg", f"[con_id={con_id}] focus"])
                return self.send_json({"ok": bool(response) and "Error" not in response}, 200 if response and "Error" not in response else 503)
            if wid.startswith("h:") and re.fullmatch(r"h:0x[0-9a-fA-F]+", wid) and shutil.which("hyprctl"):
                address = wid[2:]
                active = run(["hyprctl", "-j", "activewindow"])
                try:
                    active_address = json.loads(active).get("address", "")
                except (ValueError, AttributeError):
                    active_address = ""
                if address in _HIDDEN_HYPR_WINDOWS:
                    workspace = _HIDDEN_HYPR_WINDOWS.pop(address)
                    response = run(["hyprctl", "dispatch", "movetoworkspace", workspace + ",address:" + address])
                    run(["hyprctl", "dispatch", "focuswindow", "address:" + address])
                elif str(active_address).casefold() == address.casefold():
                    clients = run(["hyprctl", "-j", "clients"])
                    try:
                        client = next(c for c in json.loads(clients) if str(c.get("address", "")).casefold() == address.casefold())
                        workspace = str((client.get("workspace") or {}).get("name", "1"))
                    except (ValueError, StopIteration, AttributeError):
                        workspace = "1"
                    _HIDDEN_HYPR_WINDOWS[address] = workspace
                    response = run(["hyprctl", "dispatch", "movetoworkspace", "special:landdeck,address:" + address])
                else:
                    response = run(["hyprctl", "dispatch", "focuswindow", "address:" + address])
                ok = response.strip().casefold().startswith("ok")
                return self.send_json({"ok": ok}, 200 if ok else 503)
            if wid.startswith("n:") and wid[2:].isdigit() and shutil.which("niri"):
                window_id = wid[2:]
                if window_id in _HIDDEN_NIRI_WINDOWS:
                    output, workspace_index = _HIDDEN_NIRI_WINDOWS.pop(window_id)
                    if output:
                        run(["niri", "msg", "action", "focus-monitor", output])
                    response = run(["niri", "msg", "action", "move-window-to-workspace", "--window-id", window_id, "--focus", "false", workspace_index])
                else:
                    focused = run(["niri", "msg", "--json", "workspaces"])
                    try:
                        focused_data = json.loads(focused)
                        focused_data = focused_data.get("Ok", {}).get("Workspaces", focused_data) if isinstance(focused_data, dict) else focused_data
                        current_workspace = next(w for w in focused_data if w.get("is_focused"))
                        focused_id = current_workspace.get("active_window_id")
                    except (ValueError, AttributeError, StopIteration):
                        current_workspace, focused_id = {}, None
                    if str(focused_id) == window_id:
                        original_index = str(current_workspace.get("idx", ""))
                        output = str(current_workspace.get("output", ""))
                        if original_index:
                            _HIDDEN_NIRI_WINDOWS[window_id] = (output, original_index)
                            hidden_name = "Deck-Hidden-" + re.sub(r"[^A-Za-z0-9_-]", "", output or "screen")
                            listed = run(["niri", "msg", "--json", "workspaces"])
                            try:
                                ws = json.loads(listed)
                                ws = ws.get("Ok", {}).get("Workspaces", ws) if isinstance(ws, dict) else ws
                                exists = any(w.get("name") == hidden_name for w in ws)
                                empty = next((w for w in ws if w.get("output") == output and w.get("active_window_id") is None), None)
                            except (ValueError, TypeError):
                                exists, empty = False, None
                            if not exists and empty:
                                run(["niri", "msg", "action", "set-workspace-name", hidden_name, "--workspace", str(empty.get("idx"))])
                            response = run(["niri", "msg", "action", "move-window-to-workspace", "--window-id", window_id, "--focus", "false", hidden_name])
                        else:
                            response = ""
                    else:
                        response = ""
                if window_id not in _HIDDEN_NIRI_WINDOWS:
                    response = run(["niri", "msg", "action", "focus-window", "--id", window_id])
                ok = "handled" in response.casefold()
                return self.send_json({"ok": ok}, 200 if ok else 503)
            if wid.startswith("g:") and wid[2:].isdigit() and os.environ.get("WAYLAND_DISPLAY"):
                response = run(["gdbus", "call", "--session", "--dest", "io.chainit.LANDeck", "--object-path", "/io/chainit/LANDeck", "--method", "io.chainit.LANDeck.ToggleWindow", str(int(wid[2:]))], timeout=3.0)
                if response.startswith("("):
                    ok = gvariant_true(response)
                    return self.send_json({"ok": ok}, 200 if ok else 503)
                script = f"(()=>{{const w=global.get_window_actors().find(a=>a.meta_window.get_id()==={int(wid[2:])})?.meta_window;if(!w)return false;if(global.display.get_focus_window()===w)w.minimize();else w.activate(global.get_current_time());return true}})()"
                response = run(["gdbus", "call", "--session", "--dest", "org.gnome.Shell", "--object-path", "/org/gnome/Shell", "--method", "org.gnome.Shell.Eval", script], timeout=3.0)
                return self.send_json({"ok": response.startswith("(true,")}, 200 if response.startswith("(true,") else 503)
            if not wid.isdigit() or not shutil.which("xdotool"):
                return self.send_json({"error": "Window control unavailable"}, 400)
            active = run(["xdotool", "getactivewindow"])
            if active == wid:
                run(["xdotool", "windowminimize", wid])
            else:
                run(["wmctrl", "-ia", wid]) if shutil.which("wmctrl") else run(["xdotool", "windowmap", wid])
                run(["xdotool", "windowactivate", "--sync", wid])
            return self.send_json({"ok": True})
        if path == "/api/workspace":
            action = str(payload.get("action", "switch"))
            if action not in {"switch", "create"}:
                return self.send_json({"error": "Unknown workspace action"}, 400)
            ok, error = workspace_action(action, str(payload.get("monitor", "")), str(payload.get("workspace", "")))
            return self.send_json({"ok": ok, **({} if ok else {"error": error})}, 200 if ok else 503)
        if path == "/api/media/action":
            action = payload.get("action")
            if action in {"play-pause", "next", "previous", "seek", "back10", "fwd10", "volume"} and BACKEND:
                try:
                    state = BACKEND.media_action(action, str(payload.get("player", "")), payload)
                except Exception as exc:  # a media app misbehaving must not break the server
                    return self.send_json({"error": f"Media control failed: {exc}"}, 500)
                HUB.publish("media", state)
                return self.send_json({"ok": True, "media": state})
            if action not in {"play-pause", "next", "previous", "seek", "back10", "fwd10", "volume"} or not shutil.which("playerctl"):
                return self.send_json({"error": "Media control unavailable"}, 400)
            player = str(payload.get("player", ""))
            target = ["-p", player] if player and player in run(["playerctl", "-l"]).splitlines() else []
            if action in {"seek", "back10", "fwd10"}:
                try:
                    if action in {"back10", "fwd10"}:
                        chosen = player or media_state().get("player", "")
                        position = _media_player(chosen)["position"] if chosen else 0.0
                        position += -10 if action == "back10" else 10
                    else:
                        position = float(payload.get("position", 0))
                    position = max(0.0, position)
                except (TypeError, ValueError):
                    return self.send_json({"error": "Invalid position"}, 400)
                run(["playerctl", *target, "position", f"{position:.2f}"])
            elif action == "volume":
                try:
                    volume = max(0.0, min(1.0, float(payload.get("volume", 0))))
                except (TypeError, ValueError):
                    return self.send_json({"error": "Invalid volume"}, 400)
                run(["playerctl", *target, "volume", f"{volume:.2f}"])
            else:
                run(["playerctl", *target, action])
            state = media_state()
            HUB.publish("media", state)
            return self.send_json({"ok": True, "media": state})
        return self.send_json({"error": "Not found"}, 404)


# ---------------------------------------------------------------- control page (this computer only)

RESTART_HOOK = None  # set by the tray launcher so a port change can restart the server in place


def lan_addresses() -> list[str]:
    """IPv4 addresses a phone on the same network can reach; the default-route one first."""
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # picks the outgoing interface; nothing is sent
            found.append(s.getsockname()[0])
    except OSError:
        pass
    # Ask the system for its interfaces. (Resolving the host name can take ~30 s on macOS.)
    if IS_WINDOWS:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.append(info[4][0])
        except OSError:
            pass
    else:
        out = run(["ifconfig"]) if IS_MAC else run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
        found += re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    good: list[str] = []
    for ip in found:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        # 172.16-31.x.0.1 is what Docker and VM bridges give the host; phones can't reach those.
        bridge = addr in ipaddress.ip_network("172.16.0.0/12") and ip.endswith(".0.1") and good
        if not addr.is_loopback and not addr.is_link_local and not bridge and ip not in good:
            good.append(ip)
    return good or ["127.0.0.1"]


def qr_svg(text: str) -> str:
    try:
        import qrcode
        import qrcode.constants
    except ImportError:
        return ""
    q = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    q.add_data(text)
    q.make(fit=True)
    m = q.get_matrix()
    n = len(m)
    cells = "".join(f"M{x},{y}h1v1h-1z" for y, row in enumerate(m) for x, on in enumerate(row) if on)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{cells}" fill="#0b1118"/></svg>')


def capabilities() -> dict:
    if BACKEND:
        return BACKEND.capabilities()
    bridge = gnome_wayland_windows()[1] if os.environ.get("WAYLAND_DISPLAY") else False
    notes = []
    if os.environ.get("WAYLAND_DISPLAY") and "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").casefold() and not bridge:
        notes.append("Install the GNOME window bridge (install-gnome-extension.sh), then log out and in, to switch windows.")
    if not shutil.which("playerctl"):
        notes.append("Install playerctl for media controls (for example: sudo apt install playerctl).")
    return {"platform": "Linux", "windows": True, "media": bool(shutil.which("playerctl")),
            "workspaces": bool(workspace_state().get("supported")), "notes": notes}


def _admin_allowed(handler) -> bool:
    """The control page shows the PIN, so only this computer may use it, and only through a local
    host name (blocks other devices and DNS-rebinding tricks)."""
    try:
        if not ipaddress.ip_address(handler.client_address[0]).is_loopback:
            return False
    except ValueError:
        return False
    host = handler.headers.get("Host", "").strip().lower()
    host = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0]
    return host in {"127.0.0.1", "localhost", "::1"}


def _admin_get(self, path):
    if not _admin_allowed(self):
        return self.send_error(403, "The control page is only available on this computer")
    if path in ("/admin", "/admin/"):
        body = (STATIC / "admin.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return None
    if path == "/admin/api/state":
        import autostart
        urls = [f"http://{ip}:{PORT}/" for ip in lan_addresses()]
        with HUB.lock:
            clients = len(HUB.clients)
        return self.send_json({
            "version": VERSION, "pin": TOKEN, "port": PORT, "urls": urls,
            "qr": qr_svg(f"{urls[0]}#pin={TOKEN}"), "clients": clients,
            "uptime": round(time.time() - STARTED), "capabilities": capabilities(),
            "autostart": autostart.status(), "can_restart": RESTART_HOOK is not None,
            "config": str(CONFIG_FILE), "log": list(LOG)[-150:],
        })
    return self.send_error(404)


def _admin_post(self):
    global TOKEN
    # The custom header forces a CORS preflight, which this server never approves, so web pages
    # open in the browser cannot trigger these actions.
    if not _admin_allowed(self) or self.headers.get("X-Deck-Admin") != "1":
        return self.send_json({"error": "Not allowed"}, 403)
    try:
        size = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(min(size, 4096)) or b"{}")
    except (ValueError, json.JSONDecodeError):
        return self.send_json({"error": "Invalid request"}, 400)
    path = urllib.parse.urlparse(self.path).path
    config = load_config()
    if path == "/admin/api/new-pin":
        TOKEN = config["token"] = f"{secrets.randbelow(1_000_000):06d}"
        save_config(config)
        log("A new PIN was created; phones need to pair again")
        return self.send_json({"ok": True, "pin": TOKEN})
    if path == "/admin/api/autostart":
        import autostart
        ok, error = autostart.set_enabled(bool(payload.get("enabled")))
        return self.send_json({"ok": ok, "autostart": autostart.status(), **({"error": error} if error else {})}, 200 if ok else 500)
    if path == "/admin/api/port":
        try:
            port = int(payload.get("port"))
            if not 1024 <= port <= 65535:
                raise ValueError
        except (TypeError, ValueError):
            return self.send_json({"error": "Choose a port between 1024 and 65535"}, 400)
        config["port"] = port
        save_config(config)
        if RESTART_HOOK:
            threading.Timer(0.3, RESTART_HOOK, args=(port,)).start()
            return self.send_json({"ok": True, "restarting": True, "url": f"http://127.0.0.1:{port}/admin"})
        return self.send_json({"ok": True, "restarting": False, "message": "Saved. Restart LAN Deck to use the new port."})
    return self.send_json({"error": "Not found"}, 404)


Handler.admin_get = _admin_get
Handler.admin_post = _admin_post


# ---------------------------------------------------------------- platform backends

BACKEND = None


def _install_backend() -> None:
    """On Windows and macOS, replace the Linux implementations with the platform's own."""
    global BACKEND, desktop_entries, launch_desktop_app, windows, workspace_state, workspace_action, media_state, media_art
    if IS_WINDOWS:
        from backends import windows_backend as backend
    elif IS_MAC:
        from backends import macos_backend as backend
    else:
        return
    BACKEND = backend
    backend.LOG = log
    desktop_entries = backend.desktop_entries
    launch_desktop_app = backend.launch_desktop_app
    windows = backend.windows
    workspace_state = backend.workspace_state
    workspace_action = backend.workspace_action
    media_state = backend.media_state
    media_art = backend.media_art


_install_backend()


class DeckServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Phones drop connections mid-reply all the time (sleep, Wi-Fi changes): not worth a traceback.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def create_server(port: int | None = None) -> ThreadingHTTPServer:
    global PORT
    PORT = port or PORT
    server = DeckServer((os.environ.get("DECK_HOST", "0.0.0.0"), PORT), Handler)
    server.daemon_threads = True
    return server


def startup_messages() -> None:
    log(f"LAN Deck {VERSION} listening on port {PORT}")
    log(f"Pairing PIN: {TOKEN}")
    for ip in lan_addresses():
        log(f"Open http://{ip}:{PORT}/ on your phone (same Wi-Fi)")
    log(f"Control page (this computer only): http://127.0.0.1:{PORT}/admin")
    for note in capabilities().get("notes", []):
        log(note)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="LAN Deck server")
    parser.add_argument("--port", type=int, help="port to listen on (default: from settings, 8765)")
    args = parser.parse_args()
    server = create_server(args.port)
    startup_messages()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
