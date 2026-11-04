"""Windows support for LAN Deck.

- Apps:    everything in the Start menu (Get-StartApps), including Store apps; icons come from
           the Windows shell at 256 px and are cached on disk.
- Windows: top-level windows via Win32 (EnumWindows); tap to bring to front, tap again to minimize.
- Media:   the system media controls (any app that shows up in the volume flyout: Spotify,
           browsers/YouTube, ...) through WinRT. Without the WinRT packages, falls back to media keys.
- Desktops: Windows has no public API to switch virtual desktops.
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

NAME = "windows"
CREATE_NO_WINDOW = 0x08000000
DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "LAN Deck"
ICONS = DATA / "icons"
ART = DATA / "art"


LOG = print  # replaced by the server's logger (shown on the control page)


def _log(message: str) -> None:
    try:
        LOG(message)
    except Exception:
        pass


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _powershell(script: str, timeout: float = 120) -> str:
    """Run a PowerShell script without flashing a console window; returns stdout."""
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
        f.write(script)
        path = f.name
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        if p.returncode != 0 and p.stderr:
            _log("PowerShell: " + p.stderr.decode("utf-8", "replace").strip()[:300])
        return p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        _log(f"PowerShell failed: {exc}")
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------- icons (shell, 256 px, alpha kept)

_ICON_SCRIPT = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Drawing
$compiled = ($args[1] -ne 'fallback')
if ($compiled) { try {
Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
public static class DeckIcons {
  [ComImport, Guid("bcc18b79-ba16-442f-80c4-8a59c30c463b"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  private interface IShellItemImageFactory { [PreserveSig] int GetImage(SIZE size, int flags, out IntPtr phbm); }
  [StructLayout(LayoutKind.Sequential)] private struct SIZE { public int cx; public int cy; }
  [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
  private static extern void SHCreateItemFromParsingName(string path, IntPtr pbc, [MarshalAs(UnmanagedType.LPStruct)] Guid riid, [MarshalAs(UnmanagedType.Interface)] out IShellItemImageFactory factory);
  [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr h);
  public static string Save(string item, string png, int size) {
    try {
      IShellItemImageFactory f;
      SHCreateItemFromParsingName(item, IntPtr.Zero, typeof(IShellItemImageFactory).GUID, out f);
      SIZE s; s.cx = size; s.cy = size;
      IntPtr hbm;
      int hr = f.GetImage(s, 0x4, out hbm);   // SIIGBF_ICONONLY
      if (hr != 0) return "GetImage 0x" + hr.ToString("X8");
      try {
        using (Bitmap src = Image.FromHbitmap(hbm)) {
          // Copy row by row: shell bitmaps are often bottom-up (negative stride), so the
          // pixels are not one forward block starting at Scan0.
          int w = src.Width, h = src.Height;
          Rectangle r = new Rectangle(0, 0, w, h);
          byte[] px = new byte[w * h * 4];
          BitmapData sd = src.LockBits(r, ImageLockMode.ReadOnly, PixelFormat.Format32bppRgb);
          try {
            for (int y = 0; y < h; y++)
              Marshal.Copy(new IntPtr(sd.Scan0.ToInt64() + (long)y * sd.Stride), px, y * w * 4, w * 4);
          } finally { src.UnlockBits(sd); }
          bool alpha = false;
          for (int i = 3; i < px.Length; i += 4) if (px[i] != 0) { alpha = true; break; }
          if (!alpha) for (int i = 3; i < px.Length; i += 4) px[i] = 255;   // icon without transparency
          using (Bitmap dst = new Bitmap(w, h, PixelFormat.Format32bppPArgb)) {   // shell pixels are premultiplied
            BitmapData dd = dst.LockBits(r, ImageLockMode.WriteOnly, PixelFormat.Format32bppPArgb);
            try {
              for (int y = 0; y < h; y++)
                Marshal.Copy(px, y * w * 4, new IntPtr(dd.Scan0.ToInt64() + (long)y * dd.Stride), w * 4);
            } finally { dst.UnlockBits(dd); }
            dst.Save(png, ImageFormat.Png);
          }
        }
      } finally { DeleteObject(hbm); }
      return "";
    } catch (Exception e) { return e.GetType().Name + ": " + e.Message; }
  }
}
"@
} catch { $compiled = $false; Write-Output "ERR compile :: $($_.Exception.Message)" } }
# PowerShell 5.1 emits a parsed JSON array as ONE object: assign it, don't wrap it in @().
$jobs = (Get-Content -LiteralPath $args[0] -Raw -Encoding UTF8) | ConvertFrom-Json
$ok = 0
foreach ($j in $jobs) {
  $err = 'helper not compiled'
  if ($compiled) { $err = [DeckIcons]::Save($j.item, $j.png, 256) }
  if ($err -and $j.fallback) {
    # The program's own icon (smaller, but always available for an .exe)
    try {
      $icon = [System.Drawing.Icon]::ExtractAssociatedIcon($j.fallback)
      $icon.ToBitmap().Save($j.png, [System.Drawing.Imaging.ImageFormat]::Png)
      $err = ''
    } catch { $err = "$err; fallback: $($_.Exception.Message)" }
  }
  if ($err) { Write-Output "ERR $($j.item) :: $err" } else { $ok++ }
}
Write-Output "DONE $ok/$($jobs.Count)"
'''

_icon_lock = threading.Lock()
_icon_jobs: dict[str, tuple[str, str]] = {}   # png path -> (shell item, fallback .exe)
_icon_done = threading.Event()
_icon_done.set()


def _queue_icons(jobs: dict[str, tuple[str, str]]) -> None:
    """Extract icons in one PowerShell run (in the background)."""
    with _icon_lock:
        new = {png: item for png, item in jobs.items() if not Path(png).exists() and png not in _icon_jobs}
        if not new:
            return
        _icon_jobs.update(new)
        _icon_done.clear()
    threading.Thread(target=_extract_icons, daemon=True).start()


def _extract_icons() -> None:
    while True:
        with _icon_lock:
            batch = dict(_icon_jobs)
        if not batch:
            _icon_done.set()
            return
        ICONS.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump([{"item": item, "fallback": fallback, "png": png} for png, (item, fallback) in batch.items()], f)
            jobs_file = f.name
        try:
            script_file = DATA / "icons.ps1"
            DATA.mkdir(parents=True, exist_ok=True)
            script_file.write_text(_ICON_SCRIPT, encoding="utf-8-sig")
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_file), jobs_file]
            r = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=CREATE_NO_WINDOW)
            out = r.stdout.decode("utf-8", "replace").splitlines()
            if not any(l.startswith("DONE ") for l in out):
                # The high-resolution helper crashed: get each program's own icon instead.
                _log("icons: high-resolution helper failed (" + r.stderr.decode("utf-8", "replace").strip()[:200] + "); using program icons")
                r = subprocess.run(cmd + ["fallback"], capture_output=True, timeout=300, creationflags=CREATE_NO_WINDOW)
                out = r.stdout.decode("utf-8", "replace").splitlines()
            errors = [l for l in out if l.startswith("ERR ")]
            done = next((l for l in out if l.startswith("DONE ")), "DONE ?")
            _log(f"icons: saved {done[5:]}" + (f"; first problems: {' | '.join(errors[:3])}" if errors else ""))
            if r.stderr.strip():
                _log("icons (stderr): " + r.stderr.decode("utf-8", "replace").strip()[:400])
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log(f"Icon extraction failed: {exc}")
        finally:
            try:
                os.unlink(jobs_file)
            except OSError:
                pass
        with _icon_lock:
            for png in batch:
                _icon_jobs.pop(png, None)


def icon_path(icon_id: str) -> Path | None:
    """Icon file for /icon/<id>: an app id, or 'x<hash>' for a program's .exe."""
    if not re.fullmatch(r"[a-z0-9]{1,40}", icon_id):
        return None
    png = ICONS / f"{icon_id}.png"
    if not png.exists():
        _icon_done.wait(25)  # first run: icons are still being extracted
    return png if png.exists() and png.stat().st_size > 0 else None


# ---------------------------------------------------------------- apps

_APPS_SCRIPT = r'''
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
$apps = @(Get-StartApps | Select-Object Name, AppID)
$sh = New-Object -ComObject WScript.Shell
$roots = @("$env:ProgramData\Microsoft\Windows\Start Menu\Programs", "$env:APPDATA\Microsoft\Windows\Start Menu\Programs")
$links = @(foreach ($r in $roots) { Get-ChildItem -LiteralPath $r -Recurse -Filter *.lnk | ForEach-Object {
  $l = $sh.CreateShortcut($_.FullName); [pscustomobject]@{ name = $_.BaseName; lnk = $_.FullName; target = $l.TargetPath } } })
[pscustomobject]@{ apps = $apps; links = $links } | ConvertTo-Json -Depth 3 -Compress
'''

_apps_lock = threading.Lock()
_apps: tuple[float, list[dict]] = (0.0, [])
_by_exe: dict[str, dict] = {}            # lower-case exe name -> app


def _scan_apps() -> list[dict]:
    raw = _powershell(_APPS_SCRIPT)
    try:
        data = json.loads(raw[raw.index("{"):]) if "{" in raw else {}
    except ValueError:
        data = {}
    links = [l for l in (data.get("links") or []) if isinstance(l, dict)]
    start = [a for a in (data.get("apps") or []) if isinstance(a, dict) and a.get("Name") and a.get("AppID")]
    link_target = {_norm(l.get("name", "")): l.get("target", "") for l in links}
    apps: dict[str, dict] = {}
    jobs: dict[str, tuple[str, str]] = {}
    for a in start:
        app_id, name = str(a["AppID"]), str(a["Name"])
        if name.casefold() in {"uninstall", "readme", "help"} or name.casefold().startswith("uninstall "):
            continue
        key = "a" + _key(app_id)
        target = app_id if app_id.lower().endswith(".exe") else link_target.get(_norm(name), "")
        exe = Path(target.replace("\\", "/")).name.lower() if target.lower().endswith(".exe") else ""
        png = ICONS / f"{key}.png"
        jobs[str(png)] = (f"shell:AppsFolder\\{app_id}", target if target.lower().endswith(".exe") else "")
        apps[key] = {"id": key, "name": name, "icon": str(png), "icon_url": f"/icon/{key}", "wm_class": exe,
                     "exec": app_id, "categories": "", "exe": exe,
                     # Store apps: "Microsoft.WindowsTerminal_8wekyb3d8bbwe!App" -> "windowsterminal"
                     "package": _norm(app_id.split("_")[0].split(".")[-1]) if "!" in app_id else ""}
    if not start:  # Get-StartApps unavailable: use shortcuts directly
        for l in links:
            target = str(l.get("target", ""))
            if not target.lower().endswith(".exe"):
                continue
            key = "a" + _key(str(l.get("lnk", "")))
            png = ICONS / f"{key}.png"
            jobs[str(png)] = (target, target)
            apps[key] = {"id": key, "name": str(l.get("name", "")), "icon": str(png), "icon_url": f"/icon/{key}",
                         "wm_class": Path(target).name.lower(), "exec": "lnk:" + str(l.get("lnk", "")), "categories": "",
                         "exe": Path(target).name.lower(), "package": ""}
    _queue_icons(jobs)
    result = sorted(apps.values(), key=lambda a: a["name"].casefold())
    _by_exe.clear()
    for a in result:
        if a["exe"]:
            _by_exe.setdefault(a["exe"], a)
    return result


def desktop_entries() -> list[dict]:
    global _apps
    if time.monotonic() - _apps[0] < 120 and _apps[1]:
        return _apps[1]
    with _apps_lock:
        if time.monotonic() - _apps[0] >= 120 or not _apps[1]:
            _apps = (time.monotonic(), _scan_apps())
        return _apps[1]


def launch_desktop_app(app_id: str) -> tuple[bool, str]:
    app = next((a for a in desktop_entries() if a["id"] == app_id), None)
    if not app:
        return False, "App not found"
    try:
        target = app["exec"]
        if target.startswith("lnk:"):
            os.startfile(target[4:])  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{target}"], creationflags=CREATE_NO_WINDOW)
        return True, ""
    except OSError as exc:
        return False, str(exc)


# ---------------------------------------------------------------- windows (Win32)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")

WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
_GetWindowLong = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
_GetWindowLong.argtypes = [wt.HWND, ctypes.c_int]
_GetWindowLong.restype = ctypes.c_ssize_t
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

GW_OWNER, GWL_EXSTYLE = 4, -20
WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x80, 0x40000
DWMWA_CLOAKED = 14
SW_MINIMIZE, SW_RESTORE = 6, 9
_SKIP_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"}
_exe_cache: dict[int, str] = {}


def _text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _exe(pid: int) -> str:
    if pid in _exe_cache:
        return _exe_cache[pid]
    path = ""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                path = buf.value
        finally:
            kernel32.CloseHandle(h)
    if len(_exe_cache) > 2000:
        _exe_cache.clear()
    _exe_cache[pid] = path
    return path


def _cloaked(hwnd) -> bool:
    value = wt.DWORD(0)
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(value), ctypes.sizeof(value)) == 0:
        return value.value != 0
    return False


def _uwp_exe(hwnd, host_pid: int) -> str:
    """Store apps run inside ApplicationFrameHost; find the real app's process from its child window."""
    found = []

    def child(h, _):
        pid = _pid(h)
        if pid != host_pid:
            found.append(_exe(pid))
            return False
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(child), 0)
    return found[0] if found else ""


def _match_app(exe_path: str, title: str) -> dict | None:
    desktop_entries()
    exe = Path(exe_path).name.lower()
    if exe in _by_exe:
        return _by_exe[exe]
    stem = _norm(Path(exe).stem)
    if not stem:
        return None
    for a in desktop_entries():
        if a["package"] and (a["package"] == stem or stem in a["package"] or a["package"] in stem):
            return a
    for a in desktop_entries():
        if _norm(a["name"]) == stem:
            return a
    return None


def windows() -> tuple[list[dict], bool, str]:
    result: list[dict] = []
    jobs: dict[str, tuple[str, str]] = {}

    def visit(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
                return True
            ex = _GetWindowLong(hwnd, GWL_EXSTYLE)
            if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
                return True
            if _class(hwnd) in _SKIP_CLASSES or _cloaked(hwnd):
                return True
            title = _text(hwnd)
            if not title:
                return True
            pid = _pid(hwnd)
            exe = _exe(pid)
            if exe.lower().endswith("applicationframehost.exe"):
                exe = _uwp_exe(hwnd, pid) or exe
            if exe and Path(exe).name.lower() == Path(sys.executable).name.lower() and pid == os.getpid():
                return True  # our own (hidden) helper windows
            app = _match_app(exe, title)
            if app:
                app_id, app_name, icon_url = app["id"], app["name"], app["icon_url"]
            else:
                key = "x" + _key(exe.lower() or title)
                if exe:
                    jobs[str(ICONS / f"{key}.png")] = (exe, exe)
                app_id, app_name, icon_url = "", Path(exe).stem if exe else "", f"/icon/{key}" if exe else ""
            result.append({"id": f"w:{int(hwnd)}", "title": title, "class": Path(exe).name, "app_id": app_id,
                           "app_name": app_name, "icon_url": icon_url, "minimized": bool(user32.IsIconic(hwnd))})
        except Exception as exc:  # one odd window must not hide the rest
            _log(f"window skipped: {exc}")
        return True

    user32.EnumWindows(WNDENUMPROC(visit), 0)
    if jobs:
        _queue_icons(jobs)
    return result, True, ""


def activate_window(wid: str) -> tuple[bool, str]:
    """Tap = bring to front; tap on the window already in front = minimize."""
    if not wid.startswith("w:") or not wid[2:].isdigit():
        return False, "Unknown window"
    hwnd = wt.HWND(int(wid[2:]))
    if not user32.IsWindow(hwnd):
        return False, "That window is no longer open"
    fg = user32.GetForegroundWindow()
    if fg and int(fg) == int(wid[2:]) and not user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        return True, ""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    # Windows only lets the active app change focus; a synthetic Alt press lifts that lock.
    user32.keybd_event(0x12, 0, 0, 0)
    user32.SetForegroundWindow(hwnd)
    user32.BringWindowToTop(hwnd)
    user32.keybd_event(0x12, 0, 2, 0)
    return True, ""


def workspace_state() -> dict:
    return {"supported": False, "backend": "windows", "per_monitor": False, "screens": [],
            "message": "Windows doesn't let other apps switch virtual desktops."}


def workspace_action(action: str, monitor: str, workspace: str) -> tuple[bool, str]:
    return False, "Virtual desktops can't be controlled on Windows"


# ---------------------------------------------------------------- media (system media controls)

try:
    from winrt.windows.media.control import (  # type: ignore
        GlobalSystemMediaTransportControlsSessionManager as _Manager,
    )
    from winrt.windows.storage.streams import DataReader  # type: ignore
    import winrt.windows.foundation.collections  # noqa: F401  (lets Python iterate the session list)
    WINRT = True
except Exception as _exc:  # packages missing: media keys still work
    WINRT = False
    _WINRT_ERROR = str(_exc)

_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()
_sessions: dict[str, object] = {}
_art: dict[str, tuple[str, object]] = {}   # player -> (title, thumbnail reference)
_STATUS = {0: "Stopped", 1: "Stopped", 2: "Stopped", 3: "Stopped", 4: "Playing", 5: "Paused"}


def _run(coro, timeout: float = 5.0):
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True, name="winrt-media").start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout)


def _seconds(span) -> float:
    try:
        return max(0.0, span.total_seconds())
    except AttributeError:
        return max(0.0, int(getattr(span, "duration", 0)) / 10_000_000)


def _app_name(source: str) -> str:
    """'Spotify.exe' / 'MSEdge' / '308046B0AF4A39CB' -> a readable player name."""
    stem = Path(source.split("!")[0]).stem
    known = {"msedge": "edge", "chrome": "chrome", "firefox": "firefox", "spotify": "spotify", "brave": "brave"}
    low = stem.casefold()
    for k, v in known.items():
        if k in low:
            return v
    return re.sub(r"[^A-Za-z0-9]+", " ", stem).strip() or "player"


async def _snapshot() -> tuple[list[dict], str]:
    manager = await _Manager.request_async()
    current = manager.get_current_session()
    active = current.source_app_user_model_id if current else ""
    players = []
    _sessions.clear()
    now = dt.datetime.now(dt.timezone.utc)
    for s in manager.get_sessions():
        pid = s.source_app_user_model_id
        _sessions[pid] = s
        try:
            props = await s.try_get_media_properties_async()
        except Exception:
            props = None
        info = s.get_playback_info()
        status = _STATUS.get(int(info.playback_status), "Stopped") if info else "Stopped"
        tl = s.get_timeline_properties()
        length = position = 0.0
        if tl:
            length = max(0.0, _seconds(tl.end_time) - _seconds(tl.start_time))
            position = _seconds(tl.position)
            try:  # the position is only refreshed now and then; add the time since
                if status == "Playing" and tl.last_updated_time:
                    position += max(0.0, (now - tl.last_updated_time).total_seconds())
            except Exception:
                pass
            position = min(position, length) if length else position
        title = (props.title if props else "") or ""
        thumb = props.thumbnail if props else None
        if thumb:
            _art[pid] = (title, thumb)
        players.append({
            "id": pid, "name": _app_name(pid), "status": status, "playing": status == "Playing",
            "title": title, "artist": (props.artist if props else "") or "", "album": (props.album_title if props else "") or "",
            "position": position, "length": length, "volume": None,
            "art_url": f"/api/media/art?player={urllib.parse.quote(pid)}" if thumb else "",
        })
    return players, active


def media_state() -> dict:
    if not WINRT:
        keys = {"id": "keys", "name": "media keys", "status": "Unknown", "playing": False, "title": "", "artist": "",
                "album": "", "position": 0.0, "length": 0.0, "volume": None, "art_url": ""}
        return {"available": True, "playing": False, "title": "", "artist": "", "player": "keys", "players": [keys],
                "active": "keys", "time": time.time()}
    try:
        players, active = _run(_snapshot())
    except Exception as exc:
        _log(f"media read failed: {exc}")
        players, active = [], ""
    chosen = next((p for p in players if p["playing"]), next((p for p in players if p["id"] == active), players[0] if players else None))
    return {"available": True, "playing": bool(chosen and chosen["playing"]), "title": chosen["title"] if chosen else "",
            "artist": chosen["artist"] if chosen else "", "player": chosen["id"] if chosen else "", "players": players,
            "active": chosen["id"] if chosen else "", "time": time.time()}


def _media_key(vk: int) -> None:
    user32.keybd_event(vk, 0, 1, 0)        # KEYEVENTF_EXTENDEDKEY
    user32.keybd_event(vk, 0, 1 | 2, 0)    # + KEYEVENTF_KEYUP


def media_action(action: str, player: str, payload: dict) -> dict:
    if not WINRT:
        vk = {"play-pause": 0xB3, "next": 0xB0, "previous": 0xB1}.get(action)
        if vk:
            _media_key(vk)
        return media_state()

    async def act():
        manager = await _Manager.request_async()
        sessions = {s.source_app_user_model_id: s for s in manager.get_sessions()}
        s = sessions.get(player) or manager.get_current_session()
        if not s:
            return
        if action == "play-pause":
            await s.try_toggle_play_pause_async()
        elif action == "next":
            await s.try_skip_next_async()
        elif action == "previous":
            await s.try_skip_previous_async()
        elif action in {"seek", "back10", "fwd10"}:
            tl = s.get_timeline_properties()
            if action == "seek":
                target = float(payload.get("position", 0))
            else:
                target = _seconds(tl.position) + (-10 if action == "back10" else 10)
            await s.try_change_playback_position_async(int(max(0.0, target) * 10_000_000))
        # "volume": the system media controls have no per-app volume

    _run(act())
    time.sleep(0.15)
    return media_state()


async def _read_thumbnail(ref) -> bytes:
    stream = await ref.open_read_async()
    size = int(stream.size)
    reader = DataReader(stream.get_input_stream_at(0))
    await reader.load_async(size)
    try:
        return bytes(reader.read_buffer(size))
    except Exception:
        data = bytearray(size)
        reader.read_bytes(data)
        return bytes(data)


def media_art(player: str) -> Path | None:
    if not WINRT or player not in _art:
        return None
    title, ref = _art[player]
    out = ART / f"{_key(player + '|' + title)}.img"
    if not out.exists():
        try:
            data = _run(_read_thumbnail(ref))
        except Exception as exc:
            _log(f"artwork read failed: {exc}")
            return None
        ART.mkdir(parents=True, exist_ok=True)
        for old in ART.glob("*.img"):
            if old.stat().st_mtime < time.time() - 3600:
                old.unlink(missing_ok=True)
        out.write_bytes(data)
    return out


def capabilities() -> dict:
    notes = []
    if not WINRT:
        notes.append("Media details are unavailable (WinRT packages missing); play, pause and skip still work as media keys.")
    return {"platform": "Windows", "windows": True, "media": True, "workspaces": False, "notes": notes}
