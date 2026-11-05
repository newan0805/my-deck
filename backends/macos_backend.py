"""macOS support for LAN Deck.

- Apps:    .app bundles in /Applications, /System/Applications and ~/Applications, with their icons.
- Windows: one tile per running app, like the Dock (macOS gives per-window control only with
           Accessibility permission). Tap = bring to front, tap the app in front = hide it.
- Media:   Spotify and Apple Music via AppleScript (macOS has no public "now playing" API for
           other apps). macOS asks once for permission to control them.
- Desktops: Spaces can't be switched by other apps.
"""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

NAME = "macos"
DATA = Path.home() / "Library" / "Caches" / "LAN Deck"
ICONS = DATA / "icons"
ART = DATA / "art"
LOG = print  # replaced by the server's logger

try:  # PyObjC: running apps and icons without extra permissions
    from AppKit import NSWorkspace, NSBitmapImageRep, NSRunningApplication  # type: ignore
    APPKIT = True
except Exception:
    APPKIT = False


def _log(message: str) -> None:
    try:
        LOG(message)
    except Exception:
        pass


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _osascript(script: str, timeout: float = 4.0) -> str:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip() if p.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------- apps and icons

_ROOTS = [Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications",
          Path("/System/Applications/Utilities"), Path("/Applications/Utilities")]
_apps_lock = threading.Lock()
_apps: tuple[float, list[dict]] = (0.0, [])
_icon_lock = threading.Lock()
_icon_queue: dict[str, str] = {}           # png -> bundle path
_icon_done = threading.Event()
_icon_done.set()


def _bundles() -> list[Path]:
    found: list[Path] = []
    for root in _ROOTS:
        try:
            for p in root.iterdir():
                if p.suffix == ".app":
                    found.append(p)
                elif p.is_dir() and not p.name.startswith("."):  # one level of folders (e.g. "Adobe ...")
                    found.extend(q for q in p.iterdir() if q.suffix == ".app")
        except OSError:
            continue
    return found


def _info(bundle: Path) -> dict:
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def _save_icon(bundle: str, png: Path) -> bool:
    if APPKIT:
        try:
            image = NSWorkspace.sharedWorkspace().iconForFile_(bundle)
            image.setSize_((256, 256))
            rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            data = rep.representationUsingType_properties_(4, None)  # NSBitmapImageFileTypePNG
            return bool(data and data.writeToFile_atomically_(str(png), True))
        except Exception as exc:
            _log(f"icon failed for {bundle}: {exc}")
    # Without PyObjC: convert the bundle's .icns with the built-in sips tool.
    info = _info(Path(bundle))
    name = info.get("CFBundleIconFile") or ""
    if not name:
        return False
    icns = Path(bundle) / "Contents" / "Resources" / (name if name.endswith(".icns") else name + ".icns")
    r = subprocess.run(["sips", "-s", "format", "png", "--resampleHeightWidthMax", "256", str(icns), "--out", str(png)],
                       capture_output=True, timeout=20)
    return r.returncode == 0


def _icon_worker() -> None:
    while True:
        with _icon_lock:
            if not _icon_queue:
                _icon_done.set()
                return
            png, bundle = next(iter(_icon_queue.items()))
        ICONS.mkdir(parents=True, exist_ok=True)
        try:
            _save_icon(bundle, Path(png))
        except Exception as exc:
            _log(f"icon failed: {exc}")
        with _icon_lock:
            _icon_queue.pop(png, None)


def _queue_icons(jobs: dict[str, str]) -> None:
    with _icon_lock:
        new = {png: b for png, b in jobs.items() if not Path(png).exists() and png not in _icon_queue}
        if not new:
            return
        _icon_queue.update(new)
        running = not _icon_done.is_set()
        _icon_done.clear()
    if not running:
        threading.Thread(target=_icon_worker, daemon=True).start()


def icon_path(icon_id: str) -> Path | None:
    if not re.fullmatch(r"[a-z0-9]{1,40}", icon_id):
        return None
    png = ICONS / f"{icon_id}.png"
    if not png.exists():
        with _icon_lock:
            bundle = _icon_queue.get(str(png))
        if bundle:  # asked for now: convert it right away instead of waiting its turn
            ICONS.mkdir(parents=True, exist_ok=True)
            try:
                _save_icon(bundle, png)
            except Exception as exc:
                _log(f"icon failed: {exc}")
        else:
            _icon_done.wait(20)
    return png if png.exists() and png.stat().st_size > 0 else None


def _scan() -> list[dict]:
    apps: dict[str, dict] = {}
    jobs: dict[str, str] = {}
    for bundle in _bundles():
        info = _info(bundle)
        bid = str(info.get("CFBundleIdentifier") or "")
        if not bid or info.get("LSUIElement") in (True, "1") or info.get("LSBackgroundOnly") in (True, "1"):
            continue
        name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle.stem)
        key = "a" + _key(bid)
        png = ICONS / f"{key}.png"
        jobs[str(png)] = str(bundle)
        cats = str(info.get("LSApplicationCategoryType") or "")
        apps.setdefault(key, {"id": key, "name": name, "icon": str(png), "icon_url": f"/icon/{key}", "wm_class": bid,
                              "exec": str(bundle), "bundle_id": bid, "categories": _category(cats)})
    _queue_icons(jobs)
    return sorted(apps.values(), key=lambda a: a["name"].casefold())


def _category(apple: str) -> str:
    """public.app-category.developer-tools -> freedesktop-style names the web app groups by."""
    mapping = {"developer-tools": "Development", "music": "Audio", "video": "Video", "entertainment": "AudioVideo",
               "graphics-design": "Graphics", "photography": "Photography", "productivity": "Office",
               "business": "Office", "social-networking": "Chat", "utilities": "Utility", "games": "Game",
               "education": "Education", "news": "Network"}
    for k, v in mapping.items():
        if apple.endswith(k) or f".{k}" in apple:
            return v + ";"
    return ""


def desktop_entries() -> list[dict]:
    global _apps
    if time.monotonic() - _apps[0] < 120 and _apps[1]:
        return _apps[1]
    with _apps_lock:
        if time.monotonic() - _apps[0] >= 120 or not _apps[1]:
            _apps = (time.monotonic(), _scan())
        return _apps[1]


def launch_desktop_app(app_id: str) -> tuple[bool, str]:
    app = next((a for a in desktop_entries() if a["id"] == app_id), None)
    if not app:
        return False, "App not found"
    r = subprocess.run(["open", app["exec"]], capture_output=True, text=True, timeout=10)
    return (r.returncode == 0, r.stderr.strip())


# ---------------------------------------------------------------- running apps (one tile per app)

def _running() -> list[dict]:
    """[{pid, bundle_id, name, active, hidden}] for apps that show in the Dock."""
    if APPKIT:
        out = []
        for a in NSWorkspace.sharedWorkspace().runningApplications():
            if a.activationPolicy() != 0:  # NSApplicationActivationPolicyRegular
                continue
            out.append({"pid": int(a.processIdentifier()), "bundle_id": str(a.bundleIdentifier() or ""),
                        "name": str(a.localizedName() or ""), "active": bool(a.isActive()), "hidden": bool(a.isHidden())})
        return out
    raw = _osascript('tell application "System Events" to get {unix id, bundle identifier, name, frontmost, visible} '
                     'of every application process whose background only is false')
    parts = [p.strip() for p in raw.split(",")]
    n = len(parts) // 5
    return [{"pid": int(parts[i]) if parts[i].isdigit() else 0, "bundle_id": parts[n + i], "name": parts[2 * n + i],
             "active": parts[3 * n + i] == "true", "hidden": parts[4 * n + i] == "false"} for i in range(n)]


def windows() -> tuple[list[dict], bool, str]:
    by_bundle = {a["bundle_id"]: a for a in desktop_entries()}
    result = []
    for r in _running():
        if r["pid"] == os.getpid():
            continue
        app = by_bundle.get(r["bundle_id"])
        result.append({"id": f"m:{r['pid']}", "title": r["name"], "class": r["bundle_id"],
                       "app_id": app["id"] if app else "", "app_name": app["name"] if app else r["name"],
                       "icon_url": app["icon_url"] if app else "", "minimized": r["hidden"]})
    return result, True, ""


def activate_window(wid: str) -> tuple[bool, str]:
    if not wid.startswith("m:") or not wid[2:].isdigit():
        return False, "Unknown app"
    pid = int(wid[2:])
    info = next((r for r in _running() if r["pid"] == pid), None)
    if not info:
        return False, "That app is no longer running"
    if APPKIT:
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if info["active"] and not info["hidden"]:
            app.hide()
        else:
            app.unhide()
            app.activateWithOptions_(1 | 2)  # all windows, ignoring other apps
        return True, ""
    if info["active"] and not info["hidden"]:
        _osascript(f'tell application "System Events" to set visible of (first process whose unix id is {pid}) to false')
    else:
        _osascript(f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true')
    return True, ""


def workspace_state() -> dict:
    return {"supported": False, "backend": "macos", "per_monitor": False, "screens": [],
            "message": "macOS doesn't let other apps switch Spaces."}


def workspace_action(action: str, monitor: str, workspace: str) -> tuple[bool, str]:
    return False, "Spaces can't be controlled on macOS"


# ---------------------------------------------------------------- media: Spotify and Apple Music

_PLAYERS = {"spotify": ("Spotify", "com.spotify.client"), "music": ("Music", "com.apple.Music")}
_cache: tuple[float, dict] = (0.0, {})


def _is_running(bundle_id: str) -> bool:
    if APPKIT:
        return bool(NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id))
    return _osascript(f'application id "{bundle_id}" is running') == "true"


def _num(text: str) -> float:
    try:
        return float(text.replace(",", "."))  # AppleScript uses the system's decimal comma
    except ValueError:
        return 0.0


def _read(key: str) -> dict | None:
    app, bundle = _PLAYERS[key]
    if not _is_running(bundle):  # never launch a player just by asking about it
        return None
    duration = "(duration of current track) / 1000" if key == "spotify" else "duration of current track"
    art = "artwork url of current track" if key == "spotify" else '""'
    sep = "ASCII character 31"
    raw = _osascript(
        f'tell application id "{bundle}" to return (player state as string) & {sep} & (name of current track) & {sep} & '
        f'(artist of current track) & {sep} & (album of current track) & {sep} & (player position as string) & {sep} & '
        f'(({duration}) as string) & {sep} & {art} & {sep} & (sound volume as string)')
    parts = raw.split("\x1f")
    if len(parts) != 8:
        state = _osascript(f'tell application id "{bundle}" to return player state as string')
        if not state:
            return None
        parts = [state, "", "", "", "0", "0", "", "100"]
    state, title, artist, album, pos, length, art_url, volume = parts
    status = {"playing": "Playing", "paused": "Paused"}.get(state.strip().casefold(), "Stopped")
    return {"id": key, "name": app, "status": status, "playing": status == "Playing", "title": title, "artist": artist,
            "album": album, "position": _num(pos), "length": _num(length), "volume": max(0.0, min(1.0, _num(volume) / 100)),
            "art_url": art_url if art_url.startswith("https://") else ""}


def media_state() -> dict:
    global _cache
    if time.monotonic() - _cache[0] < 0.8:
        return _cache[1]
    players = [p for p in (_read(k) for k in _PLAYERS) if p]
    chosen = next((p for p in players if p["playing"]), players[0] if players else None)
    state = {"available": True, "playing": bool(chosen and chosen["playing"]), "title": chosen["title"] if chosen else "",
             "artist": chosen["artist"] if chosen else "", "player": chosen["id"] if chosen else "", "players": players,
             "active": chosen["id"] if chosen else "", "time": time.time()}
    _cache = (time.monotonic(), state)
    return state


def media_action(action: str, player: str, payload: dict) -> dict:
    global _cache
    key = player if player in _PLAYERS else (media_state().get("active") or "")
    if key not in _PLAYERS:
        return media_state()
    _app, bundle = _PLAYERS[key]
    command = {"play-pause": "playpause", "next": "next track", "previous": "previous track"}.get(action)
    if action in {"seek", "back10", "fwd10"}:
        if action == "seek":
            target = float(payload.get("position", 0))
        else:
            current = next((p for p in media_state()["players"] if p["id"] == key), {"position": 0})
            target = current["position"] + (-10 if action == "back10" else 10)
        command = f"set player position to {max(0.0, target):.2f}"
    elif action == "volume":
        command = f"set sound volume to {round(max(0.0, min(1.0, float(payload.get('volume', 0)))) * 100)}"
    if command:
        _osascript(f'tell application id "{bundle}" to {command}')
    _cache = (0.0, {})
    time.sleep(0.15)
    return media_state()


def media_art(player: str) -> Path | None:
    return None  # Spotify art is a public https URL the phone loads directly


def capabilities() -> dict:
    notes = ["Media controls work with Spotify and Apple Music. macOS asks once for permission to control them."]
    if not APPKIT:
        notes.append("Running apps need permission to control System Events (macOS will ask).")
    return {"platform": "macOS", "windows": True, "media": True, "workspaces": False, "notes": notes}
