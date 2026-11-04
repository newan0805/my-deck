#!/usr/bin/env python3
"""LAN Deck desktop launcher: runs the server with a tray / menu-bar icon.

    lan_deck.py               start (or, if already running, open the control page)
    lan_deck.py --background  start without opening the browser (used at login)
    lan_deck.py --headless    server only, no tray icon
    lan_deck.py --port 8766   use another port
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

import app as deck
import autostart


def admin_url(port: int, fragment: str = "") -> str:
    return f"http://127.0.0.1:{port}/admin" + (f"#{fragment}" if fragment else "")


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1) as r:
            return json.loads(r.read()).get("name") == "LAN Deck"
    except Exception:
        return False


class ServerThread:
    """Runs the HTTP server in the background and can restart it on another port."""

    def __init__(self, port: int):
        self.server = None
        self.thread = None
        self.start(port)

    def start(self, port: int) -> None:
        self.server = deck.create_server(port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="lan-deck-server")
        self.thread.start()
        deck.startup_messages()

    def restart(self, port: int) -> None:
        old = self.server
        try:
            self.start(port)
        except OSError as exc:
            deck.log(f"Could not move to port {port}: {exc}")
            return
        old.shutdown()
        old.server_close()

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def run_tray(server: ServerThread) -> bool:
    """Show the tray icon until Quit. Returns False if no tray is available here."""
    try:
        import pystray
        from PIL import Image
    except Exception as exc:
        deck.log(f"No tray icon ({exc}); LAN Deck keeps running in the background.")
        return False

    image = Image.open(deck.STATIC / "icon-192.png")

    def open_page(_icon=None, _item=None):
        webbrowser.open(admin_url(deck.PORT))

    def pair(_icon=None, _item=None):
        webbrowser.open(admin_url(deck.PORT, "pair"))

    def toggle_login(_icon, _item):
        autostart.set_enabled(not autostart.status().get("enabled"))

    def quit_app(icon, _item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open LAN Deck", open_page, default=True),
        pystray.MenuItem("Pair a phone…", pair),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start at login", toggle_login, checked=lambda _item: bool(autostart.status().get("enabled"))),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit LAN Deck", quit_app),
    )
    icon = pystray.Icon("lan-deck", image, "LAN Deck", menu)
    try:
        icon.run()  # blocks until Quit (must run on the main thread on macOS)
    except Exception as exc:
        deck.log(f"Tray icon failed ({exc}); LAN Deck keeps running in the background.")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="LAN Deck")
    parser.add_argument("--background", action="store_true", help="don't open the control page")
    parser.add_argument("--headless", action="store_true", help="no tray icon")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()

    port = args.port or deck.PORT
    if already_running(port):
        if not args.background:
            webbrowser.open(admin_url(port))
        return 0

    first_run = not deck.CONFIG_FILE.with_name("started").exists()
    deck.LOG_FILE = deck.CONFIG_FILE.with_name("lan-deck.log")
    try:
        server = ServerThread(port)
    except OSError as exc:
        deck.log(f"Port {port} is in use by another program: {exc}")
        return 1
    deck.RESTART_HOOK = server.restart
    try:
        deck.CONFIG_FILE.with_name("started").touch()
    except OSError:
        pass
    if (first_run or not args.background) and not os.environ.get("DECK_NO_BROWSER"):
        threading.Timer(0.8, lambda: webbrowser.open(admin_url(deck.PORT, "pair" if first_run else ""))).start()

    if args.headless or not run_tray(server):
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
