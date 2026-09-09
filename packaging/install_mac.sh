#!/bin/bash
# Build AI REFs for macOS and install it into /Applications, replacing any
# previous version.  Run from anywhere:
#
#     ./packaging/install_mac.sh            # build DMG + install the .app
#     ./packaging/install_mac.sh --no-build # reuse packaging/dist/AI REFs.app
#
# The previous copy in /Applications is moved to the Trash (not deleted), so
# it can be restored from there if needed.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="AI REFs"
APP_PATH="${SCRIPT_DIR}/dist/${APP_NAME}.app"
TARGET="/Applications/${APP_NAME}.app"

if [ "$(uname)" != "Darwin" ]; then
    echo "ERROR: this script installs a macOS app bundle and must run on macOS."
    exit 1
fi

# ── 1. Build (unless asked to reuse an existing build) ──
if [ "$1" != "--no-build" ]; then
    "${SCRIPT_DIR}/build_mac.sh"
fi

if [ ! -d "$APP_PATH" ]; then
    echo "ERROR: ${APP_PATH} not found. Run without --no-build to build it first."
    exit 1
fi

# ── 2. Quit the running app, if any ──
if pgrep -x "${APP_NAME}" >/dev/null 2>&1; then
    echo "Quitting the running ${APP_NAME}..."
    osascript -e "tell application \"${APP_NAME}\" to quit" 2>/dev/null || true
    sleep 2
fi

# ── 3. Move the previous version to the Trash ──
if [ -d "$TARGET" ]; then
    echo "Moving the previous ${APP_NAME}.app to the Trash..."
    osascript -e "tell application \"Finder\" to delete POSIX file \"${TARGET}\"" >/dev/null
fi

# ── 4. Install the new build ──
echo "Installing ${APP_NAME}.app into /Applications..."
cp -R "$APP_PATH" /Applications/

# The bundle is not code-signed; clear the quarantine flag so Gatekeeper does
# not block the first launch of a locally built app.
xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true

VERSION=$(defaults read "${TARGET}/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo "?")
echo ""
echo "=== Installed ${APP_NAME} ${VERSION} to /Applications ==="
DMG=$(ls -t "${SCRIPT_DIR}"/dist/*.dmg 2>/dev/null | head -1)
[ -n "$DMG" ] && echo "DMG installer: ${DMG}"
echo "Launching..."
open "$TARGET"
