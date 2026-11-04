"""Create build/icon.ico (Windows) and build/icon.icns (macOS) from the app icon."""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
out = ROOT / "build"
out.mkdir(exist_ok=True)
src = Image.open(ROOT / "deck" / "static" / "icon-512.png").convert("RGBA")
src.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
src.save(out / "icon.icns")
src.resize((256, 256), Image.LANCZOS).save(out / "icon-256.png")
print("icons written to", out)
