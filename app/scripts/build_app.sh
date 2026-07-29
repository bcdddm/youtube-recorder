#!/bin/bash
# Build "YouTube Recorder.app" · By Leoluchino
# Creates a double-clickable app bundle that starts the GUI server (if not
# already running) and opens it in the default browser.
set -euo pipefail

PROJ="/Users/leolinum/Coding/YouTube Recorder"
APP="$PROJ/YouTube Recorder.app"
PY=/usr/local/bin/python3

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- Info.plist ---------------------------------------------------------------
cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>YouTube Recorder</string>
    <key>CFBundleDisplayName</key><string>YouTube Recorder</string>
    <key>CFBundleIdentifier</key><string>com.leoluchino.youtube-recorder.app</string>
    <key>CFBundleVersion</key><string>0.4.19</string>
    <key>CFBundleShortVersionString</key><string>0.4.19</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>12.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHumanReadableCopyright</key><string>By Leoluchino</string>
</dict>
</plist>
EOF

# --- launcher -------------------------------------------------------------------
cat > "$APP/Contents/MacOS/launcher" <<EOF
#!/bin/bash
# Tray-resident (rumps menu bar). Window opens as a child process on demand;
# closing the window keeps the tray + server alive. Quit via the tray menu.
cd "$PROJ/app"
# arch -arm64: 防止 x86_64 父进程污染架构偏好（pydantic_core dlopen 修复）
exec /usr/bin/arch -arm64 "$PY" -m youtube_recorder.cli tray > /tmp/ytrec-applaunch.log 2>&1
EOF
chmod +x "$APP/Contents/MacOS/launcher"

# --- icon (generated with Pillow, packed with iconutil) -------------------------
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
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# --- ad-hoc sign so Gatekeeper stays quiet locally -------------------------------
codesign --force --deep -s - "$APP" 2>/dev/null || true

echo "built: $APP"
