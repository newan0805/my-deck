"""Start LAN Deck on this machine and exercise its API. Used by CI on Windows, macOS and Linux.

    python packaging/smoke_test.py                 # run from source (app.py)
    python packaging/smoke_test.py --exe PATH      # run a packaged build
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 8791
TOKEN = "424242"
failures: list[str] = []


def get(path: str, auth: bool = True, timeout: float = 30) -> tuple[int, bytes, str]:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers={"Authorization": f"Bearer {TOKEN}"} if auth else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), ""


def post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def check(ok: bool, message: str) -> None:
    print(("  ok    " if ok else "  FAIL  ") + message)
    if not ok:
        failures.append(message)


def stop(proc: subprocess.Popen) -> None:
    """Stop the server and anything it started (the one-file exe's app process, helpers)."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=15)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe")
    args = parser.parse_args()
    config = tempfile.mkdtemp(prefix="lan-deck-test-")
    env = {**os.environ, "DECK_CONFIG_DIR": config, "DECK_TOKEN": TOKEN, "DECK_NO_BROWSER": "1", "DECK_HOST": "127.0.0.1"}
    cmd = [args.exe, "--headless", "--background", "--port", str(PORT)] if args.exe else [sys.executable, "app.py", "--port", str(PORT)]
    print("starting:", " ".join(cmd))
    # Output goes to a file, not a pipe: the one-file Windows exe runs the app in a child process,
    # and a pipe held open by that child would make reading the output wait forever.
    log_path = Path(config) / "server-output.txt"
    log_file = open(log_path, "wb")
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if get("/api/health", auth=False, timeout=2)[0] == 200:
                    break
            except Exception:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        status, body, _ = get("/api/health", auth=False)
        check(status == 200 and json.loads(body).get("name") == "LAN Deck", "server answers /api/health")

        check(get("/api/apps", auth=False)[0] == 401, "API refuses requests without the PIN")
        check(get("/")[0] == 200, "web app is served")
        check(get("/admin", auth=False)[0] == 200, "control page is served on this computer")

        status, body, _ = get("/admin/api/state", auth=False)
        state = json.loads(body) if status == 200 else {}
        check(state.get("pin") == TOKEN, "control page reports the PIN")
        print("        platform:", state.get("capabilities"), " autostart:", state.get("autostart"))

        status, body, _ = get("/api/apps", timeout=180)
        apps = json.loads(body).get("apps", []) if status == 200 else []
        check(status == 200, f"apps listed ({len(apps)})")
        print("        e.g.", [a["name"] for a in apps[:8]])
        with_icons = [a for a in apps if a.get("icon_url")][:4]
        loaded = 0
        for a in with_icons:
            s, data, ctype = get(a["icon_url"], auth=False, timeout=120)
            loaded += s == 200 and len(data) > 100
        if sys.platform in ("win32", "darwin") and apps:
            check(loaded > 0, f"app icons load ({loaded}/{len(with_icons)})")
        else:
            print(f"        icons loaded: {loaded}/{len(with_icons)}")

        status, body, _ = get("/api/windows")
        data = json.loads(body) if status == 200 else {}
        check(status == 200 and "windows" in data, f"windows listed ({len(data.get('windows', []))}, supported={data.get('supported')})")
        for w in data.get("windows", [])[:6]:
            print("        window:", w.get("app_name") or "?", "|", (w.get("title") or "")[:50], "| icon" if w.get("icon_url") else "")

        status, body, _ = get("/api/media")
        media = json.loads(body) if status == 200 else {}
        check(status == 200 and "players" in media, f"media state ({len(media.get('players', []))} players)")

        status, body, _ = get("/api/workspaces")
        check(status == 200, "desktops endpoint answers")

        code, _ = post("/api/window/activate", {"id": "w:1" if sys.platform == "win32" else "m:1"})
        check(code in (200, 400, 404), f"unknown window is rejected cleanly ({code})")
        code, _ = post("/api/window/rename", {"id": "nope", "name": "x"})
        check(code == 404, "renaming a missing window is rejected")
    finally:
        stop(proc)
        log_file.close()
        out = log_path.read_bytes().decode("utf-8", "replace")
        print("---- server output (last 40 lines) ----")
        print("\n".join(out.splitlines()[-40:]))
    print("RESULT:", "FAILED: " + "; ".join(failures) if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
