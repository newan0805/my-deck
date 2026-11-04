#!/usr/bin/env python3
"""LAN Deck control panel: start/stop the background service, pair a phone, and check integrations.

Run without arguments to open the window. `--install` sets up the login service and app launcher,
`--uninstall` removes them. Settings live in ~/.config/lan-deck/config.json (shared with app.py).
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_ID = "io.chainit.LANDeckControl"
SERVICE = "lan-deck.service"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "lan-deck" / "config.json"
UNIT_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user" / SERVICE
DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
DESKTOP_FILE = DATA / "applications" / f"{APP_ID}.desktop"
ICON_FILE = DATA / "icons" / "hicolor" / "512x512" / "apps" / f"{APP_ID}.png"
EXTENSION_ID = "landdeck@chainit.local"


# ---------------------------------------------------------------- helpers (no GTK needed)

def sh(*args: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def systemctl(*args: str) -> tuple[int, str]:
    return sh("systemctl", "--user", *args)


def read_config() -> dict:
    try:
        cfg = json.loads(CONFIG_FILE.read_text())
    except (OSError, ValueError):
        cfg = {}
    changed = False
    if not (str(cfg.get("token", "")).isdigit() and len(str(cfg["token"])) == 6):
        cfg["token"] = f"{secrets.randbelow(1_000_000):06d}"
        changed = True
    if not cfg.get("port"):
        cfg["port"], changed = 8765, True
    cfg["port"] = int(cfg["port"])
    if changed:  # save now so the server and the QR code agree on the same PIN
        try:
            write_config(cfg)
        except OSError:
            pass
    return cfg


def write_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2) + "\n")
    CONFIG_FILE.chmod(0o600)


def lan_addresses() -> list[str]:
    """IPv4 addresses a phone on the same network can reach; the default-route one first."""
    primary = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
            primary = s.getsockname()[0]
    except OSError:
        pass
    found = []
    code, out = sh("ip", "-4", "-o", "addr", "show", "scope", "global")
    if code == 0:
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                ip = parts[parts.index("inet") + 1].split("/")[0]
                name = parts[1]
                if not name.startswith(("docker", "br-", "veth", "virbr")):
                    found.append(ip)
    ordered = ([primary] if primary else []) + [ip for ip in found if ip != primary]
    return ordered or ["127.0.0.1"]


def server_status(cfg: dict) -> dict | None:
    req = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/api/status", headers={"Authorization": f"Bearer {cfg['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=0.8) as r:
            return json.loads(r.read())
    except Exception:
        return None


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def install() -> str:
    """Write the login service and the app launcher, then enable and start the service."""
    write_config(read_config())
    UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNIT_FILE.write_text(f"""[Unit]
Description=LAN Deck server (phone remote for this desktop)
Documentation=file://{ROOT}/README.md
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart={sys.executable} -u {ROOT / 'app.py'}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
""")
    ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "deck" / "static" / "icon-512.png", ICON_FILE)
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(f"""[Desktop Entry]
Type=Application
Name=LAN Deck
Comment=Control the LAN Deck server and pair your phone
Exec={sys.executable} {ROOT / 'deck_control.py'}
Icon={APP_ID}
Terminal=false
Categories=Network;RemoteAccess;
Keywords=deck;remote;phone;iphone;media;
StartupNotify=true
""")
    sh("update-desktop-database", str(DESKTOP_FILE.parent))
    sh("gtk-update-icon-cache", "-q", "-t", str(DATA / "icons" / "hicolor"))
    systemctl("daemon-reload")
    code, out = systemctl("enable", SERVICE)
    if code != 0:
        return out
    port = read_config()["port"]
    if port_in_use(port) and systemctl("is-active", SERVICE)[1] != "active":
        return f"Enabled for next login, but port {port} is in use right now. Stop the LAN Deck you started in a terminal, then press Start."
    code, out = systemctl("restart", SERVICE)
    return "" if code == 0 else out


def uninstall() -> None:
    systemctl("disable", "--now", SERVICE)
    for f in (UNIT_FILE, DESKTOP_FILE, ICON_FILE):
        f.unlink(missing_ok=True)
    systemctl("daemon-reload")


# ---------------------------------------------------------------- GTK window

def run_gui() -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

    try:
        import qrcode
    except ImportError:
        qrcode = None

    CSS = b"""
    .status-dot { min-width: 12px; min-height: 12px; border-radius: 6px; background: @warning_color; }
    .status-dot.on { background: @success_color; }
    .status-dot.off { background: @error_color; }
    .hero-title { font-size: 20pt; font-weight: 800; }
    .pin { font-family: monospace; font-size: 26pt; font-weight: 800; letter-spacing: 6px; }
    .qr-frame { background: white; border-radius: 14px; padding: 10px; }
    .logs { font-family: monospace; font-size: 9pt; }
    """

    class Window(Adw.ApplicationWindow):
        def __init__(self, app: Adw.Application):
            super().__init__(application=app, title="LAN Deck", default_width=560, default_height=780)
            self.cfg = read_config()
            self.status: dict | None = None
            self.busy = False

            self.toasts = Adw.ToastOverlay()
            view = Adw.ToolbarView()
            header = Adw.HeaderBar()
            open_btn = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text="Open the deck in a browser")
            open_btn.connect("clicked", lambda *_: Gio.AppInfo.launch_default_for_uri(self.url(), None))
            header.pack_end(open_btn)
            view.add_top_bar(header)
            self.toasts.set_child(view)
            self.set_content(self.toasts)

            page = Adw.PreferencesPage()
            view.set_content(page)

            # --- Status hero
            hero = Adw.PreferencesGroup()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14, margin_top=6, margin_bottom=6)
            self.dot = Gtk.Box(css_classes=["status-dot"], valign=Gtk.Align.CENTER)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            self.hero_title = Gtk.Label(label="Checking…", xalign=0, css_classes=["hero-title"])
            self.hero_sub = Gtk.Label(label="", xalign=0, css_classes=["dim-label"], wrap=True)
            texts.append(self.hero_title); texts.append(self.hero_sub)
            self.run_switch = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Run the server")
            self.run_switch.connect("state-set", self.on_run_switch)
            box.append(self.dot); box.append(texts); box.append(self.run_switch)
            hero.add(box)
            page.add(hero)

            # --- Pairing
            pair = Adw.PreferencesGroup(title="Connect your phone", description="Scan with the iPhone camera on the same Wi-Fi. It opens the deck and pairs automatically.")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=18, margin_top=6, margin_bottom=6)
            frame = Gtk.Box(css_classes=["qr-frame"], valign=Gtk.Align.CENTER)
            self.qr = Gtk.DrawingArea(content_width=168, content_height=168)
            self.qr.set_draw_func(self.draw_qr)
            frame.append(self.qr)
            side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER, hexpand=True)
            side.append(Gtk.Label(label="PIN", xalign=0, css_classes=["caption-heading", "dim-label"]))
            self.pin_label = Gtk.Label(xalign=0, selectable=True, css_classes=["pin"])
            side.append(self.pin_label)
            self.url_label = Gtk.Label(xalign=0, selectable=True, wrap=True, wrap_mode=Pango.WrapMode.CHAR, css_classes=["dim-label"])
            side.append(self.url_label)
            btns = Gtk.Box(spacing=8)
            copy = Gtk.Button(label="Copy link", css_classes=["pill"])
            copy.connect("clicked", lambda *_: self.copy(self.url(with_pin=False), "Link copied"))
            newpin = Gtk.Button(label="New PIN", css_classes=["pill"], tooltip_text="Paired phones will need to pair again")
            newpin.connect("clicked", self.on_new_pin)
            btns.append(copy); btns.append(newpin)
            side.append(btns)
            row.append(frame); row.append(side)
            pair.add(row)
            page.add(pair)

            self.addr_group = Adw.PreferencesGroup(title="Addresses")
            self.addr_rows: list[Gtk.Widget] = []
            page.add(self.addr_group)

            # --- Service
            svc = Adw.PreferencesGroup(title="Service")
            self.autostart = Adw.SwitchRow(title="Start automatically at login", subtitle="Runs in the background, no terminal needed")
            self.autostart.connect("notify::active", self.on_autostart)
            svc.add(self.autostart)
            self.port_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
            self.port_row.set_title("Port")
            self.port_row.set_value(self.cfg["port"])
            apply_port = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER, css_classes=["flat"])
            apply_port.connect("clicked", self.on_port)
            self.port_row.add_suffix(apply_port)
            svc.add(self.port_row)
            restart = Adw.ButtonRow(title="Restart server", start_icon_name="view-refresh-symbolic")
            restart.connect("activated", lambda *_: self.service_action("restart", "Server restarted"))
            svc.add(restart)
            self.install_row = Adw.ButtonRow(title="Install background service", start_icon_name="emblem-system-symbolic", css_classes=["suggested-action"])
            self.install_row.connect("activated", self.on_install)
            svc.add(self.install_row)
            page.add(svc)

            # --- Integrations
            integ = Adw.PreferencesGroup(title="Desktop integration")
            self.bridge_row = Adw.ActionRow(title="GNOME window bridge", subtitle="Lists and switches your open windows")
            self.bridge_icon = Gtk.Image(valign=Gtk.Align.CENTER)
            self.bridge_row.add_suffix(self.bridge_icon)
            integ.add(self.bridge_row)
            self.media_row = Adw.ActionRow(title="Media controls", subtitle="playerctl")
            self.media_icon = Gtk.Image(valign=Gtk.Align.CENTER)
            self.media_row.add_suffix(self.media_icon)
            integ.add(self.media_row)
            page.add(integ)

            # --- Logs
            logs = Adw.PreferencesGroup(title="Server log")
            refresh_logs = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"], tooltip_text="Reload log")
            refresh_logs.connect("clicked", lambda *_: self.load_logs())
            logs.set_header_suffix(refresh_logs)
            scroller = Gtk.ScrolledWindow(min_content_height=180, max_content_height=240, css_classes=["card"])
            self.log_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, css_classes=["logs"],
                                         top_margin=10, bottom_margin=10, left_margin=12, right_margin=12, cursor_visible=False)
            scroller.set_child(self.log_view)
            logs.add(scroller)
            page.add(logs)

            self.render_pairing()
            self.refresh()
            self.load_logs()
            GLib.timeout_add_seconds(2, self.refresh)

        # ---- pairing
        def url(self, with_pin: bool = True) -> str:
            ip = lan_addresses()[0]
            return f"http://{ip}:{self.cfg['port']}/" + (f"#pin={self.cfg['token']}" if with_pin else "")

        def render_pairing(self):
            self.pin_label.set_label(self.cfg["token"])
            self.url_label.set_label(self.url(with_pin=False))
            self.matrix = None
            if qrcode:
                q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
                q.add_data(self.url()); q.make(fit=True)
                self.matrix = q.get_matrix()
            self.qr.queue_draw()
            for r in self.addr_rows:
                self.addr_group.remove(r)
            self.addr_rows = []
            for ip in lan_addresses():
                link = f"http://{ip}:{self.cfg['port']}/"
                row = Adw.ActionRow(title=link, subtitle="Main network" if not self.addr_rows else "Other interface", title_selectable=True)
                btn = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Copy")
                btn.connect("clicked", lambda _b, l=link: self.copy(l, "Address copied"))
                row.add_suffix(btn)
                self.addr_group.add(row)
                self.addr_rows.append(row)

        def draw_qr(self, _area, cr, width, height):
            cr.set_source_rgb(1, 1, 1); cr.paint()
            if not self.matrix:
                return
            n = len(self.matrix)
            cell = min(width, height) / n
            cr.set_source_rgb(0.04, 0.07, 0.09)
            for y, line in enumerate(self.matrix):
                for x, on in enumerate(line):
                    if on:
                        cr.rectangle(x * cell, y * cell, cell + 0.5, cell + 0.5)
            cr.fill()

        def copy(self, text: str, message: str):
            Gdk.Display.get_default().get_clipboard().set(text)
            self.toast(message)

        def toast(self, message: str):
            self.toasts.add_toast(Adw.Toast(title=message, timeout=2))

        # ---- status
        def refresh(self):
            if self.busy:
                return True
            def work():
                installed = UNIT_FILE.exists()
                active = systemctl("is-active", SERVICE)[1] == "active" if installed else False
                enabled = systemctl("is-enabled", SERVICE)[1] == "enabled" if installed else False
                status = server_status(self.cfg)
                bridge = sh("gnome-extensions", "info", EXTENSION_ID)[1] if shutil.which("gnome-extensions") else ""
                GLib.idle_add(self.apply_status, installed, active, enabled, status, bridge)
            threading.Thread(target=work, daemon=True).start()
            return True

        def apply_status(self, installed, active, enabled, status, bridge):
            self.status = status
            running = status is not None
            self.dot.remove_css_class("on"); self.dot.remove_css_class("off")
            if running:
                self.dot.add_css_class("on")
                n = status.get("clients", 0)
                up = status.get("uptime", 0)
                uptime = f"{up // 3600}h {up % 3600 // 60}m" if up >= 3600 else f"{up // 60}m {up % 60}s" if up >= 60 else f"{up}s"
                self.hero_title.set_label("Running")
                where = "as a background service" if active else "outside the service (started from a terminal)"
                self.hero_sub.set_label(f"{n} device{'s' if n != 1 else ''} connected · up {uptime} · {where}")
            else:
                self.dot.add_css_class("off")
                self.hero_title.set_label("Stopped")
                busy = port_in_use(self.cfg["port"])
                self.hero_sub.set_label(f"Port {self.cfg['port']} is used by another program, or another copy with a different PIN" if busy else "Your phone can't connect until the server runs")
            self.run_switch.handler_block_by_func(self.on_run_switch)
            self.run_switch.set_active(active); self.run_switch.set_state(active)
            self.run_switch.set_sensitive(installed)
            self.run_switch.handler_unblock_by_func(self.on_run_switch)
            self.autostart.handler_block_by_func(self.on_autostart)
            self.autostart.set_active(enabled); self.autostart.set_sensitive(installed)
            self.autostart.handler_unblock_by_func(self.on_autostart)
            self.install_row.set_visible(not installed)

            if "State: ACTIVE" in bridge:
                self.bridge_row.set_subtitle("Active — open windows are listed and switchable")
                self.bridge_icon.set_from_icon_name("object-select-symbolic")
            elif "doesn't exist" in bridge or not bridge:
                self.bridge_row.set_subtitle("Not installed — run ./install-gnome-extension.sh, then log out and in")
                self.bridge_icon.set_from_icon_name("dialog-warning-symbolic")
            else:
                state = next((l.split(":", 1)[1].strip() for l in bridge.splitlines() if l.strip().startswith("State:")), "unknown")
                self.bridge_row.set_subtitle(f"State: {state.lower()} — log out and in to load it")
                self.bridge_icon.set_from_icon_name("dialog-warning-symbolic")
            has_pc = bool(shutil.which("playerctl"))
            self.media_row.set_subtitle("playerctl found" if has_pc else "Install playerctl: sudo apt install playerctl")
            self.media_icon.set_from_icon_name("object-select-symbolic" if has_pc else "dialog-warning-symbolic")
            return False

        def load_logs(self):
            def work():
                code, out = sh("journalctl", "--user", "-u", SERVICE, "-n", "150", "--no-pager", "-o", "short-iso")
                text = out if code == 0 and out and "No entries" not in out else "No log yet. The service writes here once it runs."
                GLib.idle_add(lambda: (self.log_view.get_buffer().set_text(text), self.scroll_logs()) and False)
            threading.Thread(target=work, daemon=True).start()

        def scroll_logs(self):
            buf = self.log_view.get_buffer()
            self.log_view.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 1)

        # ---- actions
        def service_action(self, action: str, message: str):
            def work():
                code, out = systemctl(action, SERVICE)
                GLib.idle_add(lambda: (self.toast(message if code == 0 else f"Failed: {out[:80]}"), self.refresh(), self.load_logs()) and False)
            threading.Thread(target=work, daemon=True).start()

        def on_run_switch(self, _switch, state):
            self.service_action("start" if state else "stop", "Server started" if state else "Server stopped")
            return False

        def on_autostart(self, row, _pspec):
            self.service_action("enable" if row.get_active() else "disable",
                                "Will start at login" if row.get_active() else "Won't start at login")

        def on_new_pin(self, *_):
            dialog = Adw.AlertDialog(heading="Create a new PIN?", body="Phones paired with the current PIN will have to pair again (scan the new QR code).")
            dialog.add_response("cancel", "Cancel"); dialog.add_response("new", "New PIN")
            dialog.set_response_appearance("new", Adw.ResponseAppearance.DESTRUCTIVE)
            def done(d, result):
                if d.choose_finish(result) != "new":
                    return
                self.cfg["token"] = f"{secrets.randbelow(1_000_000):06d}"
                write_config(self.cfg); self.render_pairing()
                self.service_action("restart", "New PIN active")
            dialog.choose(self, None, done)

        def on_port(self, *_):
            port = int(self.port_row.get_value())
            if port == self.cfg["port"]:
                return
            self.cfg["port"] = port
            write_config(self.cfg); self.render_pairing()
            self.service_action("restart", f"Now on port {port}")

        def on_install(self, *_):
            def work():
                err = install()
                GLib.idle_add(lambda: (self.toast("Service installed and started" if not err else f"Install failed: {err[:80]}"), self.refresh(), self.load_logs()) and False)
            threading.Thread(target=work, daemon=True).start()

    def activate(app):
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = app.get_active_window() or Window(app)
        win.present()

    app = Adw.Application(application_id=APP_ID)
    app.connect("activate", activate)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    if "--install" in sys.argv:
        error = install()
        print("Installed. LAN Deck starts automatically at login." if not error else f"Service failed to start:\n{error}")
        cfg = read_config()
        print(f"PIN: {cfg['token']}   Open: http://{lan_addresses()[0]}:{cfg['port']}/")
        sys.exit(1 if error else 0)
    if "--uninstall" in sys.argv:
        uninstall()
        print("Removed the LAN Deck service and launcher. Your settings in", CONFIG_FILE, "were kept.")
        sys.exit(0)
    sys.exit(run_gui())
