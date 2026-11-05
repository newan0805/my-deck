# LAN Deck

Turn your phone into a remote for your computer. LAN Deck runs on your Windows, macOS or Linux machine and serves a touch-friendly page that any phone on the same Wi-Fi can open: a dock of your apps and open windows, an app launcher, and a music player with seek, artwork and volume.

Nothing is installed on the phone and nothing leaves your network.

## Download

Get the latest version from the [Releases page](https://github.com/newan0805/my-deck/releases).

| System | Download | Install |
|---|---|---|
| **Windows 10/11** | `LAN-Deck-…-windows-setup.exe` | Run it. No admin rights needed. A portable `…-windows-portable.exe` is also available. |
| **macOS** (Apple Silicon) | `LAN-Deck-…-macos-arm64.dmg` | Open it and drag **LAN Deck** to Applications. |
| **Linux** (x86-64) | `LAN-Deck-…-linux-x64.tar.gz` | Extract it and run `./install.sh` (no sudo). |

On first start, LAN Deck opens its **control page** in your browser. Scan the QR code with your phone's camera: the deck opens on the phone and pairs automatically. After that, LAN Deck lives in the system tray / menu bar and can start at login.

### First-run prompts

- **Windows:** Windows Defender Firewall asks whether LAN Deck may communicate on networks. Allow **Private networks** so your phone can connect. The download isn't code-signed yet, so SmartScreen may show "Windows protected your PC": choose **More info → Run anyway**.
- **macOS:** the app isn't notarized yet. The first time, right-click **LAN Deck** in Applications and choose **Open**, then **Open** again. macOS asks once for permission to control Spotify or Music the first time you use media controls, and may ask for Local Network access.
- **Linux:** install `playerctl` for media controls (`sudo apt install playerctl`). On GNOME with Wayland, run `./install-gnome-extension.sh` once and log out and in, so the deck can list and switch windows.

## What it does

- **Home dock:** your apps in the order you choose. A light under each icon shows whether it's open (bright), minimized (dim) or closed. Tap to open or bring to front; tap again to minimize. Icons never move by themselves; use **Arrange** to swap them. Rename windows (for example several terminals) from the press-and-hold menu.
- **Apps:** everything installed, with search and categories.
- **Player:** title, artist, artwork, a seek bar you can tap or drag, ±10 s, volume (where the player supports it). Tap the artwork to use it as the background.
- **Live:** changes on the computer show up on the phone instantly (Server-Sent Events).
- Works in portrait and landscape; landscape is a full-screen console-style deck.

### What works where

| | Windows | macOS | Linux |
|---|---|---|---|
| Apps list and launch | ✓ Start menu, incl. Store apps | ✓ /Applications | ✓ .desktop launchers |
| Dock: open, front, minimize | ✓ per window | ✓ per app (like the Dock) | ✓ per window (X11, GNOME, Sway, Hyprland, niri) |
| Media | ✓ any app in the system media controls (Spotify, browsers, YouTube…) | Spotify and Apple Music | ✓ any MPRIS player |
| Media volume | – | ✓ | ✓ |
| Desktops / workspaces | – (no public API) | – (no public API) | ✓ GNOME, Sway, Hyprland, niri, X11 |

## Security

Anyone with the 6-digit PIN can control the computer, so use LAN Deck on networks you trust (home Wi-Fi). Repeated wrong PINs from one device lock it out for a minute. The control page that shows the PIN only opens on the computer itself. Traffic is plain HTTP on your local network.

## Run from source

Python 3.10 or newer.

```sh
pip install -r requirements.txt   # optional: tray icon, QR code, Windows/macOS media
python lan_deck.py                # server + tray icon + control page
python app.py                     # server only, logs in the terminal
```

Settings (PIN, port, window names) are stored in:
- Windows: `%APPDATA%\LAN Deck`
- macOS: `~/Library/Application Support/LAN Deck`
- Linux: `~/.config/lan-deck`

Environment overrides: `DECK_PORT`, `DECK_TOKEN` (PIN), `DECK_HOST`, `DECK_CONFIG_DIR`.

### Linux: background service and control app

```sh
python3 deck_control.py --install     # systemd user service + "LAN Deck" app in your app grid
python3 deck_control.py --uninstall
```

The service starts at login and restarts if it crashes. The GTK control app shows the status, a pairing QR code, the PIN and port, start-at-login, integration checks and the log.

For window control on X11 install `xdotool` (and optionally `wmctrl`); on GNOME Wayland install the bridge with `./install-gnome-extension.sh`. Until the bridge is active, the deck shows running apps from their processes, but can't switch between their windows.

## Build the downloads yourself

```sh
pip install -r requirements.txt pyinstaller
python packaging/make_icons.py
pyinstaller --noconfirm --clean packaging/lan_deck.spec
python packaging/smoke_test.py --exe dist/lan-deck        # or dist/LANDeck.exe, "dist/LAN Deck.app/Contents/MacOS/LAN Deck"
```

PyInstaller builds for the system it runs on. The GitHub Actions workflow (`.github/workflows/build.yml`) builds and tests on Windows, macOS and Linux for every push; pushing a tag like `v1.0.0` publishes a release with all downloads. The Windows installer is made with Inno Setup (`packaging/windows/LANDeck.iss`).

## Project layout

| Path | What it is |
|---|---|
| `app.py` | The server (Linux support built in) |
| `backends/` | Windows and macOS support |
| `lan_deck.py` | Desktop launcher: server + tray icon |
| `autostart.py` | Start at login on each system |
| `deck/static/` | The phone web app and the control page (`admin.html`) |
| `deck_control.py` | Linux GTK control app and systemd service installer |
| `gnome-extension/` | GNOME Shell bridge for window and desktop control on Wayland |
| `packaging/` | Build recipe, installers and the cross-platform smoke test |

## iPhone tip

In Safari, tap **Share → Add to Home Screen** for a full-screen app without the address bar.

## License

MIT, see [LICENSE](LICENSE).
