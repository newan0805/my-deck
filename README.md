# LAN Deck

A Linux desktop companion you can open from an iPhone on the same Wi-Fi. It serves a touch-friendly web interface with installed apps, open windows, and desktop media controls.

## Start it

Python 3.10+ is enough for app listing and launching. For window restore/focus on an X11 session, install `xdotool`; `wmctrl` is optional. Install `playerctl` for media controls. On Debian or Ubuntu:

```sh
sudo apt install xdotool wmctrl playerctl
python3 app.py
```

On GNOME Wayland, install the one-time window bridge before starting the server:

```sh
./install-gnome-extension.sh
python3 app.py
```

The terminal prints a 6-digit numeric pairing PIN. Find the desktop's LAN IP with `hostname -I`, then open `http://<LAN-IP>:8765` on the iPhone while both devices are on the same network. Enter the PIN. Keep the terminal running while using the deck.

To choose a different port or pairing code:

```sh
DECK_PORT=8766 DECK_TOKEN=123456 python3 app.py
```

## Run it in the background (recommended)

```sh
python3 deck_control.py --install
```

This installs a systemd user service (`lan-deck.service`) that starts LAN Deck whenever you log in, restarts it if it crashes, and stops it at logout. It also adds a **LAN Deck** app to your app grid. Open it to see the server status and connected devices, scan a QR code with your phone (it opens the deck and pairs automatically), change the PIN or port, start or stop the server, and read the log.

The PIN and port are kept in `~/.config/lan-deck/config.json`, so your phone stays paired across restarts. `python3 deck_control.py --uninstall` removes the service and launcher (settings are kept).

## What works

- Reads `.desktop` launchers from `/usr/share/applications` and `~/.local/share/applications`.
- Uses each launcher's desktop icon where it can resolve it from the installed icon themes.
- Launches selected apps.
- Lists actual windows through desktop interfaces for X11, Sway/i3, Hyprland, niri, and GNOME Shell. The GNOME bridge is installed with `./install-gnome-extension.sh`; without it, Deck can see only XWayland windows. It never guesses open windows from process names.
- Tapping a window focuses/restores it; tapping the focused window again minimizes it on GNOME and X11. Sway uses its scratchpad, Hyprland uses a special workspace, and niri uses a named hidden workspace.
- Reads MPRIS players and sends play/pause, next, and previous commands through `playerctl`.
- Stores the pairing PIN in the phone browser's local storage. Eight failed pairing attempts from one address pause pairing from that address for a minute. The service is intended for a trusted home LAN; anyone with the PIN can control the desktop.
- The Dashboard shows open windows only. The installed app catalog is under the separate Apps tab. Hide individual tiles, turn off widgets/tabs, show or hide names, and change grid size from the gear menu.
- The Desktops tab groups virtual workspaces by monitor on Sway, Hyprland, and niri. GNOME and common X11 window managers share workspaces across screens, which Deck shows as shared groups.
- Live updates: the page subscribes to `/api/events` (Server-Sent Events). Track changes, play/pause and seeks are pushed the moment `playerctl --follow` reports them; window and desktop lists are checked on the server and only changes are sent. If the stream is unavailable the page falls back to polling.
- Press and hold (or right-click) any tile to pin it to Home, open a new window, or hide it. Hidden items and pins are restored from Settings.
- Includes PNG PWA icons and an Apple touch icon for iPhone Home Screen installation.

## iPhone note

The site is usable in Safari over the LAN. iOS requires a secure HTTPS context for full PWA installation and service-worker behavior, while this starter serves plain HTTP. To install it as a proper Home Screen PWA, put it behind HTTPS with a certificate the iPhone trusts. The PWA assets and manifest are already included.

## Scope

This first version is Linux-only. It does not stream the desktop screen or send keyboard/mouse input; it provides app/window selection and media controls. The server uses only Python's standard library and binds to all interfaces by default so the phone can reach it.
