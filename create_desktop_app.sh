#!/usr/bin/env bash
# Creates Nanoclaw.app on your Mac Desktop — run this once.
# The app will start the server + open the browser when double-clicked.
set -e

APP="$HOME/Desktop/Nanoclaw.app"
PROJ="$HOME/Desktop/n8n-setup/nanoclaw"

echo "Creating $APP ..."
mkdir -p "$APP/Contents/MacOS"
mkdir -p "$APP/Contents/Resources"

# ── Launcher shell script (runs when icon is double-clicked) ──────────────────
cat > "$APP/Contents/MacOS/Nanoclaw" << 'LAUNCHER'
#!/usr/bin/env bash
PROJ="$HOME/Desktop/n8n-setup/nanoclaw"
PORT=7860

# Use venv Python if present, otherwise fall back to system python3
PYTHON="$PROJ/.venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="$(which python3)"

# Find a browser (Chrome → Safari → system default)
open_browser() {
  if open -a "Google Chrome" "http://localhost:$PORT" 2>/dev/null; then return; fi
  if open -a "Safari" "http://localhost:$PORT" 2>/dev/null; then return; fi
  open "http://localhost:$PORT"
}

# If server already running, just open browser
if curl -s "http://localhost:$PORT" > /dev/null 2>&1; then
  open_browser; exit 0
fi

# Launch server: explicit cd + absolute Python path inside bash -c so nohup
# inherits the correct working directory and interpreter
nohup bash -c "cd '$PROJ' && '$PYTHON' main.py serve --port $PORT" \
  > /tmp/nanoclaw.log 2>&1 &
echo $! > /tmp/nanoclaw.pid

# Wait for server to be ready (up to 15 s)
for i in $(seq 1 30); do
  sleep 0.5
  if curl -s "http://localhost:$PORT" > /dev/null 2>&1; then
    open_browser; exit 0
  fi
done

# Timed out — open anyway so user can see the error in the browser
open_browser
LAUNCHER
chmod +x "$APP/Contents/MacOS/Nanoclaw"

# ── Info.plist ────────────────────────────────────────────────────────────────
cat > "$APP/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>          <string>Nanoclaw</string>
  <key>CFBundleDisplayName</key>   <string>Nanoclaw</string>
  <key>CFBundleIdentifier</key>    <string>com.nanoclaw.app</string>
  <key>CFBundleVersion</key>       <string>1.0</string>
  <key>CFBundleExecutable</key>    <string>Nanoclaw</string>
  <key>CFBundleIconFile</key>      <string>AppIcon</string>
  <key>CFBundlePackageType</key>   <string>APPL</string>
  <key>LSUIElement</key>           <false/>
  <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
PLIST

# ── Generate icon using Python (no Xcode required) ────────────────────────────
python3 - << 'PYICON'
import struct, zlib, base64
from pathlib import Path

# Draw a minimal PNG icon: dark background + paw emoji approximated as coloured squares
def make_png(size=512):
    """Create a simple icon PNG with dark bg + white 🐾 text via ImageFont if available."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGBA", (size, size), (15, 17, 23, 255))
        draw = ImageDraw.Draw(img)
        # Draw a gradient-ish circle
        for r in range(size//2, 0, -1):
            t = r / (size//2)
            c = int(108 * (1-t) + 15 * t), int(99 * (1-t) + 17 * t), int(255 * (1-t) + 23 * t)
            draw.ellipse([size//2-r, size//2-r, size//2+r, size//2+r], fill=c+(255,))
        # Text
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size//3)
        except Exception:
            font = ImageFont.load_default()
        draw.text((size//2, size//2), "🐾", font=font, anchor="mm", fill=(255,255,255,230))
        out = Path("/tmp/nanoclaw_icon.png")
        img.save(str(out))
        return str(out)
    except ImportError:
        return None

png = make_png()
if png:
    # Convert to icns using sips if available, otherwise copy PNG as .icns placeholder
    import subprocess, os, shutil
    iconset = "/tmp/nanoclaw.iconset"
    os.makedirs(iconset, exist_ok=True)
    for sz in [16,32,64,128,256,512]:
        subprocess.run(["sips","-z",str(sz),str(sz),png,"--out",f"{iconset}/icon_{sz}x{sz}.png"],
                       capture_output=True)
        subprocess.run(["sips","-z",str(sz*2),str(sz*2),png,"--out",f"{iconset}/icon_{sz}x{sz}@2x.png"],
                       capture_output=True)
    r = subprocess.run(["iconutil","-c","icns",iconset,"-o","/tmp/AppIcon.icns"], capture_output=True)
    if r.returncode == 0:
        shutil.copy("/tmp/AppIcon.icns",
                    f"{Path.home()}/Desktop/Nanoclaw.app/Contents/Resources/AppIcon.icns")
        print("  Icon created.")
    else:
        print("  iconutil failed — app will use default icon.")
else:
    print("  Pillow not available — app will use default icon.")
PYICON

echo ""
echo "✅  Nanoclaw.app created at $APP"
echo "   Double-click the icon on your Desktop to start Nanoclaw."
echo ""
echo "Note: On first open, macOS may show a security warning."
echo "If so: System Settings → Privacy & Security → 'Open Anyway'"
