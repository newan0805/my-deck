#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user, not with sudo." >&2
  exit 1
fi

if ! command -v gnome-extensions >/dev/null 2>&1; then
  echo "gnome-extensions is not installed; this installer is for GNOME Shell sessions." >&2
  exit 1
fi

extension_src="$(cd "$(dirname "$0")/gnome-extension" && pwd)"
bundle_dir="$(mktemp -d "${TMPDIR:-/tmp}/landdeck-bundle-XXXXXX")"
trap 'rm -rf "$bundle_dir"' EXIT
extension_dir="$bundle_dir/landdeck@chainit.local"
mkdir -p "$extension_dir"
cp "$extension_src/metadata.json" "$extension_dir/metadata.json"
cp "$extension_src/extension.js" "$extension_dir/extension.js"
shell_major="$(gnome-shell --version | sed -E 's/.* ([0-9]+)(\..*)?$/\1/')"
python3 - "$extension_dir/metadata.json" "$shell_major" <<'PY'
import json
from pathlib import Path
from sys import argv

metadata_path, shell_major = Path(argv[1]), argv[2]
metadata = json.loads(metadata_path.read_text())
versions = set(metadata.get("shell-version", []))
versions.add(shell_major)
metadata["shell-version"] = sorted(versions, key=int)
metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")
PY
gnome-extensions pack --force --out-dir "$bundle_dir" "$extension_dir"
gnome-extensions install --force "$bundle_dir/landdeck@chainit.local.shell-extension.zip"
if gnome-extensions enable landdeck@chainit.local 2>/dev/null; then
  echo "LAN Deck GNOME window bridge installed and enabled. Restart LAN Deck if it is already running."
  exit 0
fi

# GNOME Shell on Wayland only discovers new extensions at login, so `enable` fails until then.
# Add it to the enabled list directly so it starts automatically on the next login.
python3 - <<'PY'
import ast, subprocess
key = ["org.gnome.shell", "enabled-extensions"]
current = ast.literal_eval(subprocess.check_output(["gsettings", "get", *key], text=True).replace("@as ", "").strip())
if "landdeck@chainit.local" not in current:
    subprocess.check_call(["gsettings", "set", *key, str(current + ["landdeck@chainit.local"])])
PY
echo "LAN Deck GNOME window bridge installed."
echo "Log out and back in to activate it (GNOME on Wayland loads new extensions only at login)."
echo "Then check with: gnome-extensions info landdeck@chainit.local   (should say State: ACTIVE)"
