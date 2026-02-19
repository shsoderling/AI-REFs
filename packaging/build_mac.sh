#!/bin/bash
# Build AI REFs for macOS — produces a .dmg installer
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
APP_NAME="AI REFs"
VERSION="1.0.0"
DMG_NAME="${APP_NAME} ${VERSION}"
DMG_FILE="${SCRIPT_DIR}/dist/${DMG_NAME}.dmg"

echo "=== Building ${APP_NAME} ${VERSION} for macOS ==="

# ── Resolve python/pip commands ──
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "ERROR: Python not found. Install Python 3.11+ first."
    exit 1
fi

if command -v pip3 &>/dev/null; then
    PIP=pip3
elif command -v pip &>/dev/null; then
    PIP=pip
else
    PIP="$PYTHON -m pip"
fi

echo "Using: $PYTHON ($($PYTHON --version)), pip: $PIP"

# ── 1. Install dependencies ──
cd "$ROOT_DIR"
$PIP install -r requirements.txt

# Download spaCy model (optional — pipeline degrades gracefully without it)
$PYTHON -m spacy download en_core_web_sm 2>/dev/null || echo "spaCy model download skipped"

# ── 2. Build .app bundle with PyInstaller ──
cd "$SCRIPT_DIR"
$PYTHON -m PyInstaller ai_refs.spec --clean --noconfirm

APP_PATH="${SCRIPT_DIR}/dist/${APP_NAME}.app"
if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: PyInstaller did not produce ${APP_PATH}"
    exit 1
fi

echo "=== .app bundle created ==="

# ── 3. Create DMG installer ──
echo "=== Creating DMG installer ==="

# Clean previous DMG
rm -f "$DMG_FILE"

# Create a temporary directory for the DMG contents
DMG_STAGING="${SCRIPT_DIR}/dist/dmg_staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"

# Copy the .app into staging
cp -R "$APP_PATH" "$DMG_STAGING/"

# Create a symlink to /Applications for drag-and-drop install
ln -s /Applications "$DMG_STAGING/Applications"

# Try create-dmg first (produces a polished DMG with background & layout)
if command -v create-dmg &>/dev/null; then
    echo "Using create-dmg for styled installer..."
    create-dmg \
        --volname "$DMG_NAME" \
        --volicon "${ROOT_DIR}/assets/icon.icns" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "${APP_NAME}.app" 150 190 \
        --icon "Applications" 450 190 \
        --hide-extension "${APP_NAME}.app" \
        --app-drop-link 450 190 \
        --no-internet-enable \
        "$DMG_FILE" \
        "$DMG_STAGING"
else
    echo "Using hdiutil (install 'create-dmg' via Homebrew for a styled DMG)..."

    # Create a read-write DMG, set up the layout, then convert to compressed
    TEMP_DMG="${SCRIPT_DIR}/dist/temp_rw.dmg"
    rm -f "$TEMP_DMG"

    # Calculate size needed (app size + 20MB buffer)
    APP_SIZE_KB=$(du -sk "$DMG_STAGING" | cut -f1)
    DMG_SIZE_KB=$((APP_SIZE_KB + 20480))

    hdiutil create \
        -srcfolder "$DMG_STAGING" \
        -volname "$DMG_NAME" \
        -fs HFS+ \
        -fsargs "-c c=64,a=16,e=16" \
        -format UDRW \
        -size "${DMG_SIZE_KB}k" \
        "$TEMP_DMG"

    # Mount the temporary DMG
    MOUNT_DIR=$(hdiutil attach -readwrite -noverify -noautoopen "$TEMP_DMG" | \
        awk -F'\t' '/\/Volumes\//{print $NF}' | head -1)
    echo "Mounted at: $MOUNT_DIR"

    # Set custom icon on the volume
    if [ -f "${ROOT_DIR}/assets/icon.icns" ]; then
        cp "${ROOT_DIR}/assets/icon.icns" "${MOUNT_DIR}/.VolumeIcon.icns"
        SetFile -c icnC "${MOUNT_DIR}/.VolumeIcon.icns" 2>/dev/null || true
        SetFile -a C "${MOUNT_DIR}" 2>/dev/null || true
    fi

    # Set Finder window position and size via AppleScript
    osascript <<EOF
tell application "Finder"
    tell disk "$DMG_NAME"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {200, 120, 800, 520}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to 100
        set position of item "${APP_NAME}.app" of container window to {150, 190}
        set position of item "Applications" of container window to {450, 190}
        close
        open
        update without registering applications
        delay 2
        close
    end tell
end tell
EOF

    # Unmount and convert to compressed read-only DMG
    sync
    hdiutil detach "$MOUNT_DIR" -quiet
    hdiutil convert "$TEMP_DMG" -format UDZO -imagekey zlib-level=9 -o "$DMG_FILE"
    rm -f "$TEMP_DMG"
fi

# Clean up staging
rm -rf "$DMG_STAGING"

echo ""
echo "=== Build complete ==="
echo "DMG installer: ${DMG_FILE}"
echo "Size: $(du -h "$DMG_FILE" | cut -f1)"
