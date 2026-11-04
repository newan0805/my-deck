"""Start LAN Deck when the user logs in, on Windows, macOS and Linux.

- Windows: a value under HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run
- macOS:   a LaunchAgent in ~/Library/LaunchAgents
- Linux:   the systemd user service if it is installed (deck_control.py --install),
           otherwise an XDG autostart entry in ~/.config/autostart
"""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

APP_NAME = "LAN Deck"
MAC_LABEL = "io.chainit.landeck"
SYSTEMD_UNIT = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / "lan-deck.service"


def launch_command() -> list[str]:
    """How to start LAN Deck in the background (tray icon, no browser window)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--background"]
    script = str(Path(__file__).resolve().parent / "lan_deck.py")
    python = sys.executable
    if sys.platform == "win32":  # pythonw has no console window
        candidate = Path(python).with_name("pythonw.exe")
        python = str(candidate) if candidate.exists() else python
    return [python, script, "--background"]


def _windows_cmd() -> str:
    return subprocess.list2cmdline(launch_command())


def _mac_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _xdg_file() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "lan-deck.desktop"


def status() -> dict:
    """{'supported': bool, 'enabled': bool, 'method': str}"""
    try:
        if sys.platform == "win32":
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                    winreg.QueryValueEx(key, APP_NAME)
                return {"supported": True, "enabled": True, "method": "Windows sign-in"}
            except FileNotFoundError:
                return {"supported": True, "enabled": False, "method": "Windows sign-in"}
        if sys.platform == "darwin":
            return {"supported": True, "enabled": _mac_plist().exists(), "method": "macOS login item"}
        if SYSTEMD_UNIT.exists():
            r = subprocess.run(["systemctl", "--user", "is-enabled", "lan-deck.service"], capture_output=True, text=True, timeout=5)
            return {"supported": True, "enabled": r.stdout.strip() == "enabled", "method": "systemd user service"}
        return {"supported": True, "enabled": _xdg_file().exists(), "method": "desktop autostart"}
    except Exception as exc:  # never break the control page over this
        return {"supported": False, "enabled": False, "method": "", "error": str(exc)}


def set_enabled(enabled: bool) -> tuple[bool, str]:
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, _windows_cmd())
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            return True, ""
        if sys.platform == "darwin":
            plist = _mac_plist()
            if enabled:
                plist.parent.mkdir(parents=True, exist_ok=True)
                with plist.open("wb") as f:
                    plistlib.dump({"Label": MAC_LABEL, "ProgramArguments": launch_command(), "RunAtLoad": True,
                                   "ProcessType": "Interactive"}, f)
            else:
                plist.unlink(missing_ok=True)
            return True, ""
        if SYSTEMD_UNIT.exists():
            r = subprocess.run(["systemctl", "--user", "enable" if enabled else "disable", "lan-deck.service"],
                               capture_output=True, text=True, timeout=10)
            return r.returncode == 0, (r.stderr.strip() if r.returncode else "")
        desktop = _xdg_file()
        if enabled:
            desktop.parent.mkdir(parents=True, exist_ok=True)
            desktop.write_text("[Desktop Entry]\nType=Application\nName=LAN Deck\n"
                               f"Exec={shlex.join(launch_command())}\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n")
        else:
            desktop.unlink(missing_ok=True)
        return True, ""
    except Exception as exc:
        return False, str(exc)
