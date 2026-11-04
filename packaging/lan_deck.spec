# PyInstaller recipe for LAN Deck (Windows .exe, macOS .app, Linux binary).
# Build from the repository root:  pyinstaller --noconfirm --clean packaging/lan_deck.spec
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
VERSION = re.search(r'VERSION = "(.+?)"', (ROOT / "app.py").read_text()).group(1)
WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"

datas = [(str(ROOT / "deck" / "static"), "deck/static")]
hidden = ["app", "autostart", "backends", "qrcode", "qrcode.constants", "PIL.Image"]
if WINDOWS:
    hidden += ["backends.windows_backend", "pystray._win32"] + collect_submodules("winrt")
elif MAC:
    hidden += ["backends.macos_backend", "pystray._darwin", "AppKit", "Foundation"]
else:
    hidden += ["pystray._xorg", "pystray._appindicator", "pystray._gtk"]
    datas += [(str(ROOT / "gnome-extension"), "gnome-extension")]

a = Analysis([str(ROOT / "lan_deck.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=hidden,
             excludes=["tkinter", "unittest", "pydoc", "test"], noarchive=False)
pyz = PYZ(a.pure)

if MAC:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="LAN Deck", console=False,
              icon=str(ROOT / "build" / "icon.icns"))
    coll = COLLECT(exe, a.binaries, a.datas, name="LAN Deck")
    app = BUNDLE(coll, name="LAN Deck.app", icon=str(ROOT / "build" / "icon.icns"), bundle_identifier="io.chainit.landeck",
                 info_plist={
                     "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
                     "LSUIElement": True,  # menu-bar app: no Dock icon
                     "NSAppleEventsUsageDescription": "LAN Deck controls Spotify and Music when you use the remote on your phone.",
                     "NSLocalNetworkUsageDescription": "LAN Deck lets your phone on the same Wi-Fi control this Mac.",
                 })
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="LANDeck" if WINDOWS else "lan-deck",
              console=not WINDOWS, upx=False, icon=str(ROOT / "build" / "icon.ico") if WINDOWS else None)
