#!/bin/bash
# Build "YouTube Recorder.app" · By Leoluchino
#
# PyInstaller-based build: the interpreter and every dependency are bundled
# straight into the .app (see scripts/pyinstaller.spec). This replaces the
# old "thin launcher script that execs system python3" approach — that
# approach worked, but macOS's framework Python auto-relaunches itself
# through Python.framework's own Resources/Python.app the moment any
# Cocoa/AppKit GUI feature (pywebview's window) is touched, which silently
# swaps the process's Dock identity away from our bundle and back to
# Python's generic rocket icon for a moment. Embedding the interpreter
# means there's no separate framework install to get "claimed" by.
set -euo pipefail

PROJ="/Users/leolinum/Coding/YouTube Recorder"
APPDIR="$PROJ/app"
APP="$PROJ/YouTube Recorder.app"
PY=/usr/local/bin/python3
ICON="$APPDIR/scripts/AppIcon.icns"

# --- icon (generated with Pillow, packed with iconutil) -------------------------
# Written to a stable path outside $APP on purpose: PyInstaller's BUNDLE()
# step below (re)creates $APP from scratch, so anything placed inside it
# beforehand would just get wiped.
ICONSET="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$ICONSET"
"$PY" - "$ICONSET" <<'PYEOF'
import sys
from pathlib import Path
from PIL import Image, ImageDraw

out = Path(sys.argv[1])
S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
# rounded dark tile
d.rounded_rectangle([64, 64, S-64, S-64], radius=180, fill=(27, 27, 31, 255))
# red "record" ring
d.ellipse([200, 232, 480, 512], outline=(209, 96, 96, 255), width=44)
d.ellipse([288, 320, 392, 424], fill=(209, 96, 96, 255))
# amber play triangle
d.polygon([(452, 560), (452, 856), (760, 708)], fill=(224, 164, 88, 255))
# baseline bar (transcript lines)
for i, w in enumerate((330, 250, 180)):
    y = 588 + i*70
    d.rounded_rectangle([200, y, 200+w, y+34], radius=17, fill=(120, 120, 130, 255))
for size in (16, 32, 64, 128, 256, 512):
    for scale in (1, 2):
        px = size*scale
        name = f"icon_{size}x{size}" + ("@2x" if scale == 2 else "") + ".png"
        im.resize((px, px), Image.LANCZOS).save(out/name)
PYEOF
iconutil -c icns "$ICONSET" -o "$ICON"

# --- PyInstaller build ------------------------------------------------------------
cd "$APPDIR"
rm -rf "$APP" build/YouTubeRecorderSpec dist
"$PY" -m PyInstaller scripts/pyinstaller.spec \
  --distpath "$PROJ" --workpath "$(mktemp -d)" --noconfirm

# --- ad-hoc sign so Gatekeeper stays quiet locally -------------------------------
codesign --force --deep -s - "$APP" 2>/dev/null || true

echo "built: $APP"
